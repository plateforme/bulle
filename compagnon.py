"""Client du compagnon TV (Pi 3 ou PC) : micro → détection de voix → cerveau (VM .31) → voix + pilotage du visage.

  python compagnon.py                       micro par défaut, visage sur 127.0.0.1:5005
  python compagnon.py --text                questions tapées au clavier (sans micro)
  python compagnon.py --list-devices        liste les périphériques audio
Le visage (face.py) tourne à côté et reçoit les commandes UDP.
"""
import argparse, asyncio, io, json, os, queue, socket, sys, threading, time, wave

import numpy as np
import sounddevice as sd
import websockets

RATE_IN, BLOCK = 16000, 512            # 32 ms (taille attendue par Silero VAD)
PREROLL, END_SILENCE, MAX_UTT = 22, 0.75, 15.0   # PREROLL : ~700 ms gardés avant le déclenchement (le « Bulle » du début)
MIN_ON, MIN_OFF = 120, 80             # seuils de repli (détection par énergie, si Silero est absent)
VAD_ON, VAD_OFF, VAD_GAIN = 0.3, 0.2, 20.0   # Silero : probabilité de parole ; gain avant le modèle (micros faibles)
NIVEAUX_SEUIL, NIVEAUX_CALME_S = 0.10, 60    # --levels : au-dessus, une ligne par seconde ; en dessous, un battement
VAD_MODEL = "models/silero_vad.onnx"
# Coupe-parole : pouvoir interrompre Bulle pendant qu'elle parle. Desactive tant que la fuite du haut-parleur
# dans les micros n'est pas connue (jauge bulle_fuite_haut_parleur) — voir connaissances/full-duplex.md.
COUPE_ACTIF, COUPE_MARGE, COUPE_NIVEAU_MIN, COUPE_BLOCS, COUPE_APRES_S = False, 2.5, 400.0, 3, 0.6


def jeton():
    """Le secret partagé exigé par le cerveau — chaîne vide s'il manque.

    Copie volontaire de regles.jeton() : le Pi ne reçoit que face.py, carte.py, compagnon.py, tracker.py et
    regles.yaml (voir outils/deployer_pi.sh), pas les modules du cerveau. Et le jeton n'a rien à faire dans
    regles.yaml, qui est versionné et déployé.
    """
    j = os.environ.get("BULLE_JETON", "").strip()
    if j: return j
    try:
        with open(os.path.expanduser("~/.config/bulle/jeton"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _nuit_reglages():
    try:
        import yaml
        return yaml.safe_load(open(os.environ.get("BULLE_REGLES", os.path.expanduser("~/kinectface/regles.yaml")), encoding="utf-8")).get("nuit") or {}
    except Exception:
        return {}


def _dans_la_nuit(r, maintenant=None):
    """Vrai si l'heure locale est dans la plage de nuit (qui passe minuit)."""
    if not r.get("auto", True): return False
    m = maintenant or time.localtime()
    h = m.tm_hour * 60 + m.tm_min
    deb = sum(int(x) * f for x, f in zip(str(r.get("debut", "23:00")).split(":"), (60, 1)))
    fin = sum(int(x) * f for x, f in zip(str(r.get("fin", "07:00")).split(":"), (60, 1)))
    return (h >= deb or h < fin) if deb > fin else (deb <= h < fin)


def _regles_client():
    """Réglages lus dans ~/kinectface/regles.yaml (section « client ») ; les constantes ci-dessus servent de repli."""
    global VAD_ON, VAD_OFF, VAD_GAIN, PREROLL, END_SILENCE, NIVEAUX_SEUIL, NIVEAUX_CALME_S
    global COUPE_ACTIF, COUPE_MARGE, COUPE_NIVEAU_MIN, COUPE_BLOCS, COUPE_APRES_S
    try:
        import yaml
        r = yaml.safe_load(open(os.environ.get("BULLE_REGLES", os.path.expanduser("~/kinectface/regles.yaml")), encoding="utf-8")).get("client") or {}
    except Exception:
        return {}
    VAD_ON, VAD_OFF, VAD_GAIN = r.get("vad_seuil_on", VAD_ON), r.get("vad_seuil_off", VAD_OFF), r.get("vad_gain", VAD_GAIN)
    PREROLL, END_SILENCE = r.get("preroll_blocs", PREROLL), r.get("fin_silence_s", END_SILENCE)
    NIVEAUX_SEUIL = r.get("niveaux_seuil", NIVEAUX_SEUIL)
    NIVEAUX_CALME_S = r.get("niveaux_calme_s", NIVEAUX_CALME_S)
    cp = r.get("coupe_parole") or {}
    COUPE_ACTIF = bool(cp.get("actif", COUPE_ACTIF))
    COUPE_MARGE, COUPE_NIVEAU_MIN = cp.get("marge", COUPE_MARGE), cp.get("niveau_min", COUPE_NIVEAU_MIN)
    COUPE_BLOCS, COUPE_APRES_S = cp.get("blocs", COUPE_BLOCS), cp.get("apres_s", COUPE_APRES_S)
    return r
MAX_LAG = 12                           # écart max entre micros Kinect (22,6 cm ≈ 10,5 échantillons à 16 kHz) + marge


def gcc_phat(x, ref, max_lag=MAX_LAG):
    """Retard (échantillons, fractionnaire) de x par rapport à ref, et netteté du pic (GCC-PHAT, bande 300-4000 Hz)."""
    n = 1 << int(np.ceil(np.log2(len(x) + len(ref))))
    X, R = np.fft.rfft(x, n), np.fft.rfft(ref, n)
    G = X * np.conj(R)
    G /= np.abs(G) + 1e-9
    f = np.fft.rfftfreq(n, 1 / RATE_IN)
    G[(f < 300) | (f > 4000)] = 0
    cc = np.fft.irfft(G, n)
    cc = np.concatenate([cc[-max_lag:], cc[:max_lag + 1]])
    i = int(np.argmax(cc))
    frac = 0.0
    if 0 < i < len(cc) - 1:
        a, b, c = cc[i - 1], cc[i], cc[i + 1]
        den = a - 2 * b + c
        frac = 0.5 * (a - c) / den if den else 0.0
    sharp = float(cc[i] / (np.mean(np.abs(cc)) + 1e-9))
    return i - max_lag + frac, sharp


def frac_shift(x, tau):
    """Avance x de tau échantillons (fractionnaire, par déphasage)."""
    n = len(x)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n)
    return np.fft.irfft(X * np.exp(2j * np.pi * f * tau), n)


class Beamformer:
    """Formation de faisceau « délai et somme » orientée par la voix : les retards entre micros sont mesurés sur chaque
    énoncé (GCC-PHAT) puis servent à réaligner les canaux ; la voix s'additionne en phase, le bruit de la pièce non."""

    def __init__(self, nch):
        self.nch, self.tau = nch, np.zeros(nch)          # tau[k] : retard du micro k sur le micro 0
        self.hist = np.zeros((BLOCK, nch), np.float32)
        self.estimates = 0

    def block(self, data):
        """Bloc temps réel (entiers) pour la détection de voix : retards arrondis, léger retard global."""
        buf = np.vstack([self.hist, data.astype(np.float32)])
        self.hist = buf[-BLOCK:]
        t = np.round(self.tau).astype(int)
        T = int(t.max())
        out = np.zeros(BLOCK, np.float32)
        for k in range(self.nch):
            s0 = BLOCK + t[k] - T
            out += buf[s0:s0 + BLOCK, k]
        return (out / self.nch).astype(np.int16)

    def utterance(self, multi):
        """Énoncé complet : mesure les retards sur cette voix, met à jour l'orientation, rend le signal aligné."""
        x = multi.astype(np.float32)
        new, sharp = np.zeros(self.nch), []
        for k in range(1, self.nch):
            d, sh = gcc_phat(x[:, k], x[:, 0])
            new[k] = d
            sharp.append(sh)
        if min(sharp) > 3.5:                              # pic net : mesure fiable
            self.tau = new if self.estimates == 0 else 0.5 * self.tau + 0.5 * new
            self.estimates += 1
        out = np.zeros(len(x), np.float32)
        for k in range(self.nch):
            out += frac_shift(x[:, k], self.tau[k]) if k else x[:, 0]
        return (out / self.nch).astype(np.int16), new, sharp


class SileroVAD:
    """Détecteur de parole neuronal (Silero v5, ONNX) : distingue une voix lointaine d'un bruit, ce que l'énergie ne sait pas."""

    def __init__(self, path):
        import onnxruntime as ort
        o = ort.SessionOptions(); o.intra_op_num_threads = 1; o.inter_op_num_threads = 1
        self.s = ort.InferenceSession(path, sess_options=o, providers=["CPUExecutionProvider"])
        self.sr = np.array(RATE_IN, np.int64)
        self.reset()

    def reset(self):
        self.state, self.ctx = np.zeros((2, 1, 128), np.float32), np.zeros(64, np.float32)

    def __call__(self, x):
        f = np.clip(x.astype(np.float32) / 32768 * VAD_GAIN, -1, 1)
        out, self.state = self.s.run(None, {"input": np.concatenate([self.ctx, f])[None], "state": self.state, "sr": self.sr})
        self.ctx = f[-64:]
        return float(out[0][0])
WINDOW = 15.0                          # s d'écoute sans « Bulle » après une réponse (fenêtre de conversation)
HOLD = 2.5                             # s pendant lesquelles l'émotion de la réponse reste affichée
DESOLE_S = 12.0                        # s pendant lesquelles la mine désolée d'une panne reste affichée


class FaceLink:
    def __init__(self, addr):
        host, port = addr.split(":")
        self.addr, self.sock = (host, int(port)), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, **kw):
        try: self.sock.sendto(json.dumps(kw).encode(), self.addr)
        except OSError: pass


class Player:
    """Joue les WAV reçus à la suite et envoie le niveau de la voix au visage (synchro labiale)."""

    def __init__(self, face, device=None):
        self.face, self.buf, self.lock, self.rate = face, np.zeros(0, np.float32), threading.Lock(), 24000
        self.stream, self.device, self.last_audio = None, device, 0.0
        self.gain = 1.0                      # baissé en mode nuit

    def _open(self, rate):
        if self.stream and self.rate == rate: return
        if self.stream: self.stream.close()
        self.rate = rate
        self.stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32", blocksize=int(rate * 0.03),
                                      callback=self._cb, device=self.device)
        self.stream.start()

    def _cb(self, out, frames, t, status):
        with self.lock:
            n = min(frames, len(self.buf))
            out[:n, 0] = self.buf[:n] * self.gain; out[n:, 0] = 0
            self.buf = self.buf[n:]
        if n:
            rms = float(np.sqrt(np.mean(out[:n, 0] ** 2)))
            self.face.send(talking=True, mouth=min(1.0, rms * 7))
            self.last_audio = time.time()

    def add_wav(self, data):
        with wave.open(io.BytesIO(data)) as w:
            rate, ch = w.getframerate(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        if ch > 1: pcm = pcm.reshape(-1, ch).mean(1)
        self._open(rate)
        with self.lock:
            self.buf = np.concatenate([self.buf, pcm])

    def couper(self):
        """Arrête net ce qui reste à dire → secondes jetées. Premiere marche du full-duplex : sans ça, « Bulle,
        tais-toi » attend quand meme la fin de la phrase en cours, puis toutes les suivantes deja en file."""
        with self.lock:
            reste, self.buf = len(self.buf) / self.rate, np.zeros(0, np.float32)
        self.last_audio = 0.0                     # busy() doit redevenir faux tout de suite, pas dans 350 ms
        self.face.send(talking=False, mouth=0.0)
        return reste

    def busy(self):
        with self.lock: pending = len(self.buf)
        return pending > 0 or time.time() - self.last_audio < 0.35


class Fuite:
    """Ce que le haut-parleur renvoie dans les micros pendant que Bulle parle.

    C'est LE chiffre qui conditionne tout le full-duplex. Aujourd'hui le micro est coupé tant que Bulle parle
    (`muted`), sans quoi elle s'entend et se répond. Le rouvrir demande de savoir de combien une vraie voix
    doit dépasser sa propre fuite — et ce nombre dépend du volume, de la place de la Kinect devant la TV et de
    la pièce. Il ne se devine pas : on le mesure d'abord, on règle le seuil ensuite.

    On ne garde que le pic sur une fenêtre glissante, pas l'historique : c'est un seuil qu'on cherche, pas une
    courbe, et le Pi 3 est déjà bridé thermiquement une partie de la journée.
    """

    FENETRE = 1800.0

    def __init__(self):
        self._pic, self._quand = 0.0, 0.0

    def bloc(self, rms):
        maintenant = time.time()
        if maintenant - self._quand > self.FENETRE: self._pic = 0.0
        if rms >= self._pic: self._pic, self._quand = rms, maintenant

    def pic(self):
        """→ le pic de fuite récent, ou 0 si Bulle n'a pas parlé depuis une demi-heure."""
        return self._pic if time.time() - self._quand <= self.FENETRE else 0.0


class Niveaux:
    """Le journal du niveau micro (--levels), taillé pour ce qu'on en fait vraiment.

    Ces lignes ne servent qu'à l'analyste, qui compte les secondes où la probabilité de parole tombe entre 0,15
    et le seuil de déclenchement (incident « parole_sous_seuil », fiche voix-trop-faible). En dessous de ce
    seuil, une ligne n'apprend rien — et il y en avait UNE PAR SECONDE, jour et nuit : 86 000 par jour écrites
    sur la carte SD du Pi, à décrire surtout du silence.

    On écrit donc chaque seconde qui dépasse le seuil, et pour les autres un battement toutes les `calme_s`
    secondes. Ce battement n'est pas un détail : sans lui, un micro mort ressemblerait exactement à une pièce
    calme. Il porte le MAXIMUM de la période, ce qu'il faut aussi pour vérifier qu'il n'y a pas de faux
    déclenchements en silence (fiche voix-trop-faible : « parole < 0,05 »).

    Le seuil est sous les 0,15 de l'analyste, avec de la marge : aucune seconde qu'il compte ne peut se perdre
    dans un battement.
    """

    def __init__(self, seuil=0.10, calme_s=60):
        self.seuil, self.calme_s, self.calme = seuil, calme_s, []

    def seconde(self, niveau, parole, plancher):
        """Une seconde de micro → la ligne à écrire, ou None s'il n'y a rien à dire."""
        if parole >= self.seuil:
            self.calme = []
            return f"niveau {niveau:6.0f}  plancher {plancher:5.0f}  parole {parole:.2f}"
        self.calme.append((niveau, parole))
        if len(self.calme) < self.calme_s:
            return None
        n, pmax, secondes = (max(a for a, _ in self.calme), max(b for _, b in self.calme), len(self.calme))
        self.calme = []
        return f"niveau {n:6.0f}  plancher {plancher:5.0f}  parole {pmax:.2f}  ({secondes} s calmes)"


class Listener:
    """Détection de voix par énergie avec plancher de bruit adaptatif ; se coupe pendant que l'IA parle."""

    def __init__(self, device=None, channel=None, levels=False):
        self.q, self.channel, self.levels = queue.Queue(), channel, levels
        self.niveaux = Niveaux(NIVEAUX_SEUIL, NIVEAUX_CALME_S)
        self.vad = None
        try:
            self.vad = SileroVAD(os.path.join(os.path.dirname(os.path.abspath(__file__)), VAD_MODEL))
            print("détection de voix : Silero", flush=True)
        except Exception as e:
            print("détection de voix : énergie (Silero indisponible :", e, ")", flush=True)
        n = sd.query_devices(device, "input")["max_input_channels"]
        nch = min(n, 4) if channel is None else channel + 1   # Kinect : 4 micros, moyennés
        self.stream = sd.InputStream(samplerate=RATE_IN, channels=nch, dtype="int16", blocksize=BLOCK,
                                     device=device, callback=self._cb)

        self.beam = Beamformer(nch) if channel is None and nch >= 3 else None
        self.angle = None                                 # angle de la personne selon le suivi Kinect (journal)
        self.present, self.distance = None, None          # derniere presence annoncee par le suivi Kinect
        self.fuite = Fuite()                              # ce que le haut-parleur renvoie dans les micros
        self.parle_depuis = 0.0                           # debut de la phrase en cours de Bulle (coupe-parole)
        # Ce qu'on a mesure du dernier enonce SANS en ecouter les mots : c'est tout ce que le cerveau recevra en
        # plus de l'audio. Rien ici ne peut porter une parole — que des nombres (voir server.SIGNAUX).
        self.signaux = {}

    def _seuil_coupe(self):
        """Le niveau qu'une voix doit dépasser pour couper Bulle : sa propre fuite, avec de la marge.

        Le plancher existe pour le cas où elle vient de démarrer et n'a encore rien mesuré — sans lui, le seuil
        vaudrait zéro et le premier craquement de parquet la ferait taire.
        """
        return max(COUPE_NIVEAU_MIN, self.fuite.pic() * COUPE_MARGE)

    def _cb(self, data, frames, t, status):
        if self.channel is not None: self.q.put((data[:, self.channel].copy(), None))
        elif self.beam: self.q.put((None, data.copy()))
        elif data.shape[1] > 1: self.q.put((data.astype(np.int32).mean(axis=1).astype(np.int16), None))
        else: self.q.put((data[:, 0].copy(), None))

    def start(self): self.stream.start()

    def utterances(self, muted, on_start, on_coupe=None, on_niveau=None):
        """Générateur bloquant : rend chaque énoncé (int16) terminé par un silence.

        `muted()` rend « voix » quand Bulle parle, « attente » quand elle réfléchit, « » sinon. La distinction
        compte : c'est seulement pendant « voix » qu'on mesure la fuite du haut-parleur, et c'est seulement là
        qu'on peut se faire couper la parole.
        """
        floor, pre, cur, speaking, loud, quiet = 150.0, [], [], False, 0, 0.0
        pk_rms, pk_vad = 0.0, 0.0                         # pic de niveau et de probabilite de parole sur l'enonce
        dernier_niveau = 0.0                              # dernier envoi du niveau au visage (trait d'écoute)
        haut, prec = 0, ""                                # blocs forts d'affilee pendant que Bulle parle, et etat precedent
        mpre, mcur = [], []                               # mêmes blocs, en 4 canaux (formation de faisceau)
        while True:
            x, multi = self.q.get()
            if multi is not None: x = self.beam.block(multi)
            rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2)))
            m = muted()
            if m != prec:
                # Bulle vient de commencer a dire une phrase : c'est de la que compte COUPE_APRES_S. Le
                # compteur repart a chaque phrase (la file se vide entre deux, et `muted` repasse par
                # « attente ») — c'est plus prudent que l'inverse, et `apres_s` se regle a chaud.
                if m == "voix": self.parle_depuis = time.time()
                haut, prec = 0, m
            if m:
                if m == "voix":
                    self.fuite.bloc(rms)
                    # Silero n'aiderait pas ici : la synthèse de Bulle EST une voix, il la reconnaîtrait comme
                    # telle. Seul le niveau sépare quelqu'un qui parle de ce que le haut-parleur renvoie — et
                    # c'est aussi ce qui coûte le moins cher au Pi, qui fait déjà tourner le visage à 30 ips.
                    fort = COUPE_ACTIF and rms > self._seuil_coupe() and time.time() - self.parle_depuis >= COUPE_APRES_S
                    haut = haut + 1 if fort else 0
                    if haut >= COUPE_BLOCS and on_coupe:
                        haut = 0
                        on_coupe(rms, self._seuil_coupe())
                if pre or cur or speaking:
                    pre.clear(); cur.clear(); mpre.clear(); mcur.clear(); speaking = False; loud = 0
                    if self.vad: self.vad.reset()
                continue
            # Le trait d'écoute du visage réagit à la voix entendue, pas à une sinusoïde : on lui passe le
            # niveau du bloc, normalisé comme `mouth` (rms sur [-1, 1] × 7). Quinze envois par seconde au plus,
            # deux fois moins que les blocs et déjà au-delà de ce que l'œil distingue.
            # Rien n'est journalisé ici : c'est une amplitude, pas du texte, mais on ne crée pas une trace de
            # plus de ce qui n'était peut-être pas adressé à Bulle.
            if on_niveau and time.time() - dernier_niveau >= 1 / 15.0:
                dernier_niveau = time.time()
                on_niveau(min(1.0, rms / 32768 * 7))
            p = self.vad(x) if self.vad else None
            if self.levels:
                self._acc = getattr(self, "_acc", []) + [(rms, p or 0.0)]
                if len(self._acc) >= 31:                    # ~1 s de blocs de 32 ms
                    ligne = self.niveaux.seconde(max(a for a, _ in self._acc), max(b for _, b in self._acc), floor)
                    if ligne: print(ligne, flush=True)
                    self._acc = []
            if speaking:
                pk_rms, pk_vad = max(pk_rms, rms), max(pk_vad, p or 0.0)
            if p is not None: is_on, is_off = p > VAD_ON, p < VAD_OFF
            else: is_on, is_off = rms > max(floor * 3.0, MIN_ON), rms < max(floor * 1.8, MIN_OFF)
            if not speaking:
                floor = 0.97 * floor + 0.03 * min(rms, floor * 2)
                pre.append(x); pre[:] = pre[-PREROLL:]
                if multi is not None: mpre.append(multi); mpre[:] = mpre[-PREROLL:]
                loud = loud + 1 if is_on else 0
                if loud >= (2 if p is not None else 3):
                    speaking, cur, mcur, quiet = True, list(pre), list(mpre), 0.0
                    pk_rms, pk_vad = rms, p or 0.0
                    on_start()
            else:
                cur.append(x)
                if multi is not None: mcur.append(multi)
                quiet = quiet + BLOCK / RATE_IN if is_off else 0.0
                dur = len(cur) * BLOCK / RATE_IN
                if quiet >= END_SILENCE or dur >= MAX_UTT:
                    speaking, loud = False, 0
                    utt = np.concatenate(cur)
                    self.signaux = {"duree_s": round(len(utt) / RATE_IN, 2), "niveau": round(pk_rms, 1),
                                    "vad": round(pk_vad, 3), "angle_kinect": self.angle,
                                    "distance_m": self.distance, "presence": self.present,
                                    "fuite": round(self.fuite.pic(), 1)}
                    if self.beam and mcur:
                        try:
                            utt, new, sharp = self.beam.utterance(np.concatenate(mcur))
                            # Les retards entre micros disent d'ou venait la voix ; l'angle Kinect dit ou etait
                            # la personne. Les deux ensemble, c'est de quoi apprendre un jour a reconnaitre un
                            # enonce adresse a Bulle sans qu'elle soit nommee — a condition de les garder.
                            self.signaux.update(retards=[float(d) for d in new[1:]], nettete=[float(v) for v in sharp])
                            ang = "" if self.angle is None else f" | suivi Kinect : {self.angle:+.0f}°"
                            print("       (faisceau : retards " + " ".join(f"{d:+.1f}" for d in new[1:]) +
                                  " éch., netteté " + " ".join(f"{v:.0f}" for v in sharp) + ang + ")", flush=True)
                        except Exception as e:
                            print("       (faisceau indisponible :", e, ")", flush=True)
                    yield utt
                    cur, pre, mcur, mpre = [], [], [], []


def normalize(pcm, peak=0.7, max_gain=24.0):
    """Remonte l'énoncé à un bon niveau pour Whisper (les micros Kinect bruts sont très faibles)."""
    x = pcm.astype(np.float32)
    m = float(np.percentile(np.abs(x), 99.9)) or 1.0
    g = min(max_gain, peak * 32767 / m)
    return np.clip(x * g, -32767, 32767).astype(np.int16)


def to_wav(pcm):
    pcm = normalize(pcm)
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE_IN); w.writeframes(pcm.tobytes())
    return b.getvalue()


async def run(a):
    face = FaceLink(a.face)
    player = Player(face, a.out_device)
    state = {"waiting": False, "listening": False, "last": time.time(), "emotion": None, "fstate": None,
             "awake_until": 0.0, "present": None, "absent_since": 0.0, "nuit": None, "nuit_manuel": None,
             "coupe": False, "emotion_jusqu": 0.0}
    reg_nuit = _nuit_reglages()

    def appliquer_nuit(actif, manuel=False):
        """Mode nuit : visage en veille, voix baissée, nom obligatoire."""
        if manuel: state["nuit_manuel"] = actif
        if state["nuit"] == actif: return
        state["nuit"] = actif
        face.send(nuit=actif, luminosite=float(reg_nuit.get("luminosite_ecran", 0.15)))
        player.gain = float(reg_nuit.get("volume_voix", 0.45)) if actif else 1.0
        if actif: state["awake_until"] = 0.0
        print("mode nuit" if actif else "mode jour", flush=True)
    awake = lambda: time.time() < state["awake_until"]

    def set_emotion(e, duree=None):
        """Ce que ressent Bulle (neutre, joie, desole… ; étiquettes du LLM acceptées).

        `duree` = émotion PASSAGÈRE, effacée après ce délai. Sans ça, la tête des mauvais jours restait à
        l'écran jusqu'à la conversation suivante : une panne de deux minutes et Bulle boudait toute l'après-midi
        (21/09, Greg : « il fait la gueule depuis tout à l'heure »).
        """
        state["emotion_jusqu"] = time.time() + duree if duree else 0.0
        if state["emotion"] != e:
            state["emotion"] = e; face.send(emotion=e)

    def set_fstate(st):   # ce que fait Bulle : idle | listening | thinking (speaking est piloté par la voix)
        if state["fstate"] != st:
            state["fstate"] = st; face.send(state=st)

    def open_window():
        if state["nuit"] and reg_nuit.get("exiger_nom", True): return   # la nuit, il faut dire « Bulle »
        state["awake_until"] = time.time() + WINDOW; state["last"] = time.time()
        set_emotion("neutre"); set_fstate("listening")
    set_emotion("neutre"); set_fstate("idle")

    async with websockets.connect(a.server, max_size=None, ping_interval=20,
                                  additional_headers={"Authorization": "Bearer " + a.jeton}) as ws:
        print("connecté au cerveau :", a.server)

        async def receiver():
            try:
                await _receive()
            except websockets.exceptions.ConnectionClosed as e:
                # le cerveau a redémarré (mise à jour) : on quitte proprement, systemd relance le client aussitôt
                print("connexion au cerveau fermée (", e.rcvd.code if e.rcvd else "?", ") : redémarrage du client", flush=True)
                os._exit(75)

        async def _receive():
            async for m in ws:
                if isinstance(m, bytes):
                    # Coupee, Bulle se tait — mais le cerveau, lui, finit sa reponse : le LLM ecrit encore et
                    # la synthese suit. On jette les phrases qui arrivent jusqu'au « done ». Consequence a
                    # garder en tete : l'echange est journalise comme s'il avait ete dit en entier, et il
                    # reste dans l'historique de conversation. Bulle croit avoir dit ce qu'on ne l'a pas
                    # laissee finir. Le reparer demande de pouvoir arreter le cerveau, pas seulement le son.
                    if not state["coupe"]: player.add_wav(m)
                    continue
                d = json.loads(m)
                t = d.get("type")
                if t == "transcript": print("toi  :", d["text"])
                elif t == "ignored":
                    state["waiting"] = False
                    # on n'affiche plus ce qui a été entendu : ce journal part dans journalctl, qui le garde
                    # des semaines, et une phrase qui n'était pas pour Bulle n'a rien à y faire
                    if d.get("reason") == "pas_nomme": print("       (pas pour moi)")
                    set_fstate("listening" if awake() else "idle")
                elif t == "wake": print("       (Bulle t'écoute)"); state["waiting"] = False; open_window()
                elif t == "thinking": set_fstate("thinking")
                elif t == "tool": print("       (outil :", d["name"] + ")"); set_fstate("thinking")
                elif t == "emotion": set_emotion(d["emotion"])
                elif t == "mode" and "nuit" in d: appliquer_nuit(bool(d["nuit"]), manuel=True)
                elif t == "carte":     # ce que la voix dit mal : liste, chiffres, orthographe
                    print("       (carte :", ((d.get("carte") or {}).get("gabarit") or "effacée") + ")")
                    face.send(carte=d.get("carte"))
                elif t == "sentence": print("IA   :", d["text"])
                elif t == "error":
                    # la mine désolée dit la panne, mais elle ne doit pas s'installer : elle s'efface seule
                    print("erreur :", d["message"]); set_emotion("desole", duree=DESOLE_S); set_fstate("idle")
                elif t == "done": state["waiting"], state["coupe"] = False, False
                state["last"] = time.time()

        async def idle():
            talking = False
            while True:
                await asyncio.sleep(0.1)
                busy = player.busy()
                auto = _dans_la_nuit(reg_nuit)
                if state["nuit_manuel"] is not None and state["nuit_manuel"] != auto:
                    if state["nuit"] != state["nuit_manuel"]: appliquer_nuit(state["nuit_manuel"])
                elif state["nuit"] != auto:
                    state["nuit_manuel"] = None; appliquer_nuit(auto)   # l'horaire reprend la main au changement de plage
                if busy and state["fstate"] == "thinking": state["fstate"] = "speaking"   # le visage passe seul en parole
                if talking and not busy:
                    face.send(talking=False, mouth=0)
                    if not state["waiting"]:
                        state["awake_until"] = time.time() + WINDOW  # on peut enchaîner sans redire « Bulle »
                        state["fstate"] = None; set_fstate("listening")
                talking = busy
                idle_for = time.time() - state["last"]
                if busy or state["waiting"] or state["listening"]:
                    state["last"] = time.time()
                elif awake():
                    if idle_for > HOLD: set_emotion("neutre")
                else:
                    if state["fstate"] == "listening": set_fstate("idle")
                    gone = state["present"] is False and time.time() - state["absent_since"] > a.sleep_absent
                    if gone or (state["present"] is None and idle_for > a.sleep_after): set_emotion("sommeil")
                    elif idle_for > HOLD: set_emotion("neutre")

        async def signals():
            """Signaux locaux du module Kinect (UDP) : {"presence": bool}, {"hand": true} = écoute sans « Bulle »."""
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", a.signal_port)); sock.setblocking(False)
            while True:
                try:
                    d = json.loads(await loop.sock_recv(sock, 1024))
                except (ValueError, OSError):
                    await asyncio.sleep(0.05); continue
                if "angle" in d and lis_ref: lis_ref[0].angle = d["angle"]
                if "presence" in d:
                    state["present"] = bool(d["presence"])
                    if lis_ref:
                        lis_ref[0].present = state["present"]
                        lis_ref[0].distance = d.get("dist") if state["present"] else None
                    if state["present"]:
                        print("       (quelqu'un est là)"); state["last"] = time.time()
                        if state["emotion"] == "sommeil" and not state["nuit"]: set_emotion("neutre")
                    else:
                        print("       (plus personne)"); state["absent_since"] = time.time()
                if d.get("hand") and not state["waiting"] and not player.busy():
                    print("       (main levée : Bulle t'écoute)"); open_window()

        async def battement():
            """Dit au visage que le cerveau répond encore, toutes les 5 s.

            Sans ça, le visage ne savait rien d'une panne : quand le cerveau tombe, on sort en 75, systemd
            relance le client en boucle, et la TV continue d'afficher une Bulle attentive qui n'écoute plus
            rien. Cette tâche vit DANS le `async with` : elle s'arrête avec la websocket, ce qui est
            exactement le signal qu'on veut transmettre.
            """
            while True:
                face.send(lien=True)
                # une émotion passagère (la mine d'une panne) s'efface ici, mais jamais pendant que Bulle parle
                if state["emotion_jusqu"] and time.time() > state["emotion_jusqu"] and not player.busy():
                    set_emotion("sommeil" if state["nuit"] else "neutre")
                await asyncio.sleep(5)

        lis_ref = []
        loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(receiver()), asyncio.create_task(idle()), asyncio.create_task(signals()),
                 asyncio.create_task(battement())]

        if a.text:
            while True:
                q = await loop.run_in_executor(None, sys.stdin.readline)
                if not q:  # fin de l'entrée (test scripté) : on attend la fin de la réponse
                    while state["waiting"] or player.busy(): await asyncio.sleep(0.2)
                    break
                if q.strip():
                    state["waiting"] = True; state["last"] = time.time()
                    await ws.send(json.dumps({"type": "text", "text": q.strip()}))
        else:
            lis = Listener(a.in_device, a.channel, a.levels)
            lis_ref.append(lis)
            lis.start()
            # « voix » quand Bulle parle, « attente » quand elle reflechit : c'est pendant « voix », et la
            # seulement, qu'on mesure la fuite du haut-parleur et qu'on peut se faire couper la parole.
            muted = lambda: "voix" if player.busy() else ("attente" if state["waiting"] else "")

            def on_start():
                state["listening"] = True
                if awake(): state["last"] = time.time(); set_fstate("listening")

            def on_coupe(niveau, seuil):
                """Quelqu'un a parle par-dessus Bulle, nettement plus fort que sa propre fuite : elle se tait.

                On rouvre la fenetre de conversation dans la foulee : quand on coupe la parole a quelqu'un,
                c'est pour lui dire quelque chose — exiger « Bulle » juste apres serait absurde.
                """
                reste = player.couper()
                state["coupe"], state["waiting"] = True, False
                print(f"       (on me coupe : {niveau:.0f} > {seuil:.0f}, {reste:.1f} s jetees)", flush=True)
                loop.call_soon_threadsafe(open_window)

            def on_niveau(niveau):
                """Le niveau entendu, pour le trait d'écoute du visage. Seulement pendant `listening` : ailleurs
                le trait n'est pas affiché, et pendant que Bulle parle ce serait sa propre fuite qu'on animerait."""
                if state["fstate"] == "listening":
                    face.send(ecoute=niveau)

            aq: asyncio.Queue = asyncio.Queue()

            def pump():
                for utt in lis.utterances(muted, lambda: loop.call_soon_threadsafe(on_start), on_coupe, on_niveau):
                    loop.call_soon_threadsafe(aq.put_nowait, utt)
            threading.Thread(target=pump, daemon=True).start()
            print("j'écoute… (Ctrl+C pour quitter)")
            while True:
                utt = await aq.get()
                state["listening"] = False
                if len(utt) / RATE_IN < 0.5: continue
                was_awake = awake() and not (state["nuit"] and reg_nuit.get("exiger_nom", True))
                state["waiting"], state["coupe"] = True, False
                if was_awake: state["last"] = time.time(); set_fstate("thinking")
                await ws.send(json.dumps({"type": "audio", "awake": was_awake, "signaux": lis.signaux}))
                await ws.send(to_wav(utt))
        for t in tasks: t.cancel()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="ws://192.0.2.31:8802/ws")
    ap.add_argument("--face", default="127.0.0.1:5005")
    ap.add_argument("--text", action="store_true", help="questions tapées au clavier")
    ap.add_argument("--in-device", default=None, help="index ou nom du micro")
    ap.add_argument("--out-device", default=None, help="index ou nom de la sortie audio")
    ap.add_argument("--channel", type=int, default=None, help="canal du micro à garder (Kinect : 0 à 3)")
    ap.add_argument("--sleep-after", type=float, default=120, help="sans module Kinect : secondes d'inactivité avant de s'endormir")
    ap.add_argument("--sleep-absent", type=float, default=45, help="avec la Kinect : secondes sans personne avant de s'endormir")
    ap.add_argument("--signal-port", type=int, default=5006, help="port UDP local des signaux Kinect (main levée)")
    ap.add_argument("--levels", action="store_true", help="journalise le niveau du micro (pour l'analyste)")
    ap.add_argument("--list-devices", action="store_true")
    a = ap.parse_args()
    global WINDOW
    r = _regles_client()
    WINDOW = r.get("fenetre_conversation_s", WINDOW)
    if "endormi_apres_absence_s" in r: a.sleep_absent = r["endormi_apres_absence_s"]
    print(f"réglages : seuil voix {VAD_ON}/{VAD_OFF}, gain {VAD_GAIN}, fenêtre {WINDOW} s, préroll {PREROLL} blocs", flush=True)
    a.jeton = jeton()
    if not a.jeton and not a.list_devices:
        # sans jeton le cerveau refuse la connexion : autant le dire clairement ici plutôt que de boucler sur
        # un 403 que systemd relancerait toutes les deux secondes
        sys.exit("aucun jeton : écrire le secret dans ~/.config/bulle/jeton (chmod 600) ou définir BULLE_JETON")
    if a.list_devices: print(sd.query_devices()); return
    for k in ("in_device", "out_device"):
        v = getattr(a, k)
        if v is not None and v.isdigit(): setattr(a, k, int(v))
    if a.in_device is None and not a.text:  # sur le Pi : la Kinect si elle est là
        if any("Kinect" in d["name"] and d["max_input_channels"] for d in sd.query_devices()): a.in_device = "Kinect"
    try: asyncio.run(run(a))
    except KeyboardInterrupt: pass
    except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as e:
        # Cerveau injoignable : depuis le 21/09 ce n'est plus une anomalie du client, c'est un état que le
        # visage AFFICHE (écran hors ligne). Une ligne suffit donc. Avant, chaque tentative ratée versait une
        # trace complète dans le journal — quinze lignes toutes les quinze secondes, dans un journal
        # persistant, et au milieu exactement de ce qu'on vient y chercher quand le cerveau est tombé.
        # Même code de sortie que la déconnexion en cours de route : systemd relance dans deux secondes.
        print(f"cerveau injoignable ({type(e).__name__}) : nouvelle tentative dans un instant", flush=True)
        sys.exit(75)


if __name__ == "__main__":
    main()
