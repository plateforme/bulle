"""Visage de Bulle pour la TV — direction « noir & blanc + un accent ».

Principe : deux yeux blancs (le personnage) sur fond noir ; une seule couleur d'accent, corail, réservée à ce que
fait Bulle (écoute, réflexion, parole) et aux joues quand il est touché. Pas de bouche : la parole se lit sur un
« fil » corail sous les yeux — un seul élément, qui change de forme : trait pendant l'écoute, point qui va et
vient pendant la réflexion, onde pendant la parole.

Commandes UDP (port 5005), un objet JSON par datagramme, clés combinables :
  {"emotion": "joie"}          neutre, joie, tendre, clin, surprise, curieux, serieux, desole, sommeil
                               (les anciens noms — happy, shy, worried… — sont convertis)
  {"state": "listening"}       idle | listening | thinking | speaking
  {"nuit": true, "luminosite": 0.15}   veille de nuit : visage assombri (facteur), sommeil au repos
  {"gaze": [x, y]}             regard vers l'utilisateur, x/y dans [-1, 1] (x>0 = droite de l'écran) ; null = libère
  {"talking": true, "mouth": 0.6}   niveau de la voix [0, 1] → amplitude du fil de voix
  {"ecoute": 0.4}              niveau ENTENDU [0, 1] → longueur et intensité du trait d'écoute ; retombe seul
  {"lien": true}               battement du compagnon (5 s) ; sans lui pendant 20 s, le visage se dit hors ligne
  {"carte": {...}}             ce que la voix dit mal (liste, chiffres, orthographe) — voir carte.py ; null = efface
                               le visage se décale dans le tiers opposé et jette un coup d'œil à la carte

Rendu : shader GPU (formes à distance signée) → bords exacts à la définition de la TV, halo dégradé, peu de CPU.
Clavier : 1-9 = émotions, L/T/S/I = états, flèches = regard, M maintenu = parole simulée, C = cartes, Espace = démo, Échap.
Planche : python face.py --grid planche.png
"""
import argparse, json, math, os, random, socket

import pygame

import carte

WHITE = (242, 238, 230)                       # blanc chaud : le personnage
ACCENT = (255, 106, 77)                       # corail : l'état du système et la tendresse
BG = (0, 0, 0)
NUIT_LUMINOSITE = 0.3                         # facteur de luminosité en veille de nuit
BLANC_N = tuple(v / 255 for v in WHITE)       # le blanc du personnage, normalisé pour le shader
AMBRE = (1.0, 0.78, 0.55)                     # vers quoi ce blanc tire en veille de nuit : une lampe, pas un écran
# Quatre battements manqués. Le compagnon envoie « lien » toutes les 5 s tant que la websocket est ouverte ;
# en dessous, une seconde de réseau capricieux ferait clignoter l'écran hors ligne en pleine conversation.
HORS_LIGNE_S = 20.0

# --- Retouches visuelles du 21/09 (lot 4 de la refonte). Purement affaire d'œil : elles ne se jugent qu'à
# 3,5 m de la TV, et aucun test ne peut le faire à notre place. Elles sont nommées ici, puis injectées dans le
# shader (« // @reglages »), pour qu'un retour en arrière tienne en une ligne.
HALO_ETENDUE = 5.0      # décroissance du halo autour de l'œil, en K. 7,0 avant : il bavait et noyait le contour.
HALO_FORCE = 0.24       # intensité de ce halo. 0,32 avant.
REFLET = 0.0            # reflet dans la pupille. 1,0 avant : il faisait bille de manga à côté du reste. La
                        # pupille, elle, reste — c'est elle qui porte le regard suivi par la Kinect.
CARTE_PART = 2 / 3.0                          # part de l'écran prise par la carte ; le visage occupe le reste
CARTE_ZOOM = 0.62                             # le visage rétrécit pour tenir dans son tiers
CARTE_FONDU = 0.35                            # apparition / disparition (s)
CARTE_DUREE = 15.0                            # durée par défaut (s) ; 0 = jusqu'au remplacement
ECOUTE_REPLI_S = 2.0                          # sans niveau d'écoute depuis ce délai, le trait respire à nouveau

EMOTIONS = ["neutre", "joie", "tendre", "clin", "surprise", "curieux", "serieux", "desole", "sommeil"]
STATES = ["idle", "listening", "thinking", "speaking"]
LEGACY = {  # étiquettes du LLM et anciens noms → nouvelle palette
    "default": "neutre", "default_still": "neutre", "pretending": "neutre", "interested": "curieux",
    "expecting": "curieux", "questioning": "curieux", "doubting": "curieux", "aware_l": "neutre", "aware_r": "neutre",
    "happy": "joie", "active": "joie", "singing": "joie", "proud": "joie", "confident": "joie",
    "pleased": "clin", "shy": "tendre", "innocent": "tendre", "shocked": "surprise",
    "serious": "serieux", "impatient": "serieux", "helpless": "desole", "worried": "desole",
    "tired": "sommeil", "lazy": "sommeil",
}

# Paramètres (unités « 640 × 360 ») : w/h = taille des yeux, top = paupière haute (0-1), tilt = angle de la paupière
# (+ = coin intérieur bas, sérieux ; − = coin extérieur bas, triste), smile = arc bas (yeux en croissant ^ ^),
# dy_* = décalage vertical (tête penchée), sz_* = taille relative, bright = intensité.
BASE = dict(w=76.0, h=102.0, top=0.0, tilt=0.0, smile=0.0, dy_l=0.0, dy_r=0.0, sz_l=1.0, sz_r=1.0,
            smile_r=0.0, look_x=0.0, look_y=0.0, bright=1.0, blush=0.0, bounce=0.0, spread=0.0, pupil=1.0)
PRESETS = {
    "neutre":   {},
    "joie":     dict(smile=0.55, bounce=1.0, look_y=-0.1),
    "tendre":   dict(smile=0.48, blush=1.0, w=70, h=94, spread=-8, look_y=0.15, pupil=1.25),
    "clin":     dict(smile_r=0.55, look_y=-0.1, bounce=0.5),
    "surprise": dict(w=90, h=94, bright=1.08, spread=6, pupil=0.62),
    "curieux":  dict(dy_l=-7, dy_r=6, sz_l=1.07, sz_r=0.94, look_x=0.1, look_y=-0.15),
    "serieux":  dict(top=0.34, tilt=15, h=94),
    "desole":   dict(top=0.36, tilt=-17, look_y=0.35, h=96),
    "sommeil":  dict(top=0.9, bright=0.6, look_y=0.25),
}


def _col(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


class Face:
    def __init__(self, W, H):
        self.W, self.H, self.K = W, H, W / 640
        self.emotion, self.state = "neutre", "idle"
        self.cur, self.target = dict(BASE), dict(BASE)
        self.track, self.track_age = None, 99.0
        self.sacc, self.next_sacc = (0.0, 0.0), 1.0
        self.blink_t, self.next_blink = -1.0, 2.5
        self.level, self.level_s, self.talking = 0.0, 0.0, False
        # Écoute : le niveau entendu par le compagnon. `ecoute_age` sert de repli — sans message depuis
        # ECOUTE_REPLI_S, on retombe sur la respiration d'avant (visage en --demo, ou ancien compagnon).
        self.ecoute, self.ecoute_s, self.ecoute_age = 0.0, 0.0, 99.0
        self.nuit, self.nuit_k, self.nuit_t = False, 1.0, 0.0   # veille de nuit : luminosité, puis teinte (0 = jour)
        # Lien avec le cerveau. `lien_age` part à zéro : au démarrage, le compagnon met quelques secondes à se
        # connecter, et annoncer une panne avant de lui avoir laissé sa chance serait faux.
        self.lien_age, self.hors_ligne, self.vide, self.sans_lien = 0.0, False, 0.0, False
        self.nuit_luminosite = NUIT_LUMINOSITE       # réglé par le client depuis config/regles.yaml
        self.status_a = {"listening": 0.0, "thinking": 0.0, "speaking": 0.0}
        self.phase = 0.0
        self.carte, self.carte_t, self.carte_sig = None, 0.0, 0   # carte affichée, son âge, compteur de changements
        self.glance = 0.0                            # coup d'œil vers la carte qui vient d'apparaître
        self.cx, self.zoom = 0.5, 1.0                # centre et échelle du visage (animés quand la carte arrive)
        self._rebuild()

    # ------------------------------------------------------------ commandes
    def set_emotion(self, name):
        name = LEGACY.get(str(name).lower(), str(name).lower())
        if name in PRESETS:
            self.emotion = name; self._rebuild()

    def set_state(self, st):
        if st in STATES:
            self.state = st; self._rebuild()

    def _rebuild(self):
        t = dict(BASE, **PRESETS[self.emotion])
        if self.state == "listening":          # attentif : yeux un peu plus grands et lumineux
            t["w"] *= 1.06; t["h"] *= 1.06; t["bright"] *= 1.08
        elif self.state == "thinking" and self.emotion != "sommeil":   # regard en l'air, paupière un peu baissée
            t.update(look_x=0.55, look_y=-0.6, top=max(t["top"], 0.14))
        self.target = t

    def command(self, msg):
        if "emotion" in msg: self.set_emotion(msg["emotion"])
        if "state" in msg: self.set_state(msg["state"])
        if "gaze" in msg:
            g = msg["gaze"]
            self.track = None if g is None else (max(-1, min(1, float(g[0]))), max(-1, min(1, float(g[1]))))
            self.track_age = 0.0
        if "talking" in msg:
            self.talking = bool(msg["talking"])
            if self.talking and self.state != "speaking": self.set_state("speaking")
            if not self.talking and self.state == "speaking": self.set_state("idle")
        if "mouth" in msg: self.level = max(0.0, min(1.0, float(msg["mouth"])))
        if "lien" in msg: self.lien_age = 0.0        # battement du compagnon : le cerveau répond encore
        if "ecoute" in msg:
            self.ecoute, self.ecoute_age = max(0.0, min(1.0, float(msg["ecoute"]))), 0.0
        if "luminosite" in msg:
            self.nuit_luminosite = max(0.0, min(1.0, float(msg["luminosite"])))
        if "nuit" in msg:
            self.nuit = bool(msg["nuit"])
            if self.nuit and self.state == "idle": self.set_emotion("sommeil")
            elif not self.nuit and self.emotion == "sommeil": self.set_emotion("neutre")
        if "carte" in msg: self.set_carte(msg["carte"])

    def set_carte(self, c):
        self.carte = dict(c) if isinstance(c, dict) and c.get("gabarit") else None
        self.carte_t, self.carte_sig = 0.0, self.carte_sig + 1
        if self.carte: self.glance = 1.0                        # elle regarde ce qu'elle vient d'afficher

    def carte_cote(self):
        return -1.0 if (self.carte or {}).get("cote") == "gauche" else 1.0

    def carte_alpha(self):
        """Fondu d'entrée, puis de sortie juste avant l'échéance."""
        if not self.carte: return 0.0
        duree = float(self.carte.get("duree", CARTE_DUREE))
        # la carte n'apparaît qu'une fois la place faite : sinon elle se dessine par-dessus le visage qui se range
        place = (abs(self.cx - 0.5) / max(1e-3, 0.5 - (1 - CARTE_PART) / 2) - 0.7) / 0.2
        a = min(1.0, self.carte_t / CARTE_FONDU, max(0.0, place))
        if duree: a = min(a, max(0.0, (duree - self.carte_t) / CARTE_FONDU))
        return a * self.nuit_k * (1 - self.vide)

    def snap(self):
        self.cur = dict(self.target)
        for k in self.status_a: self.status_a[k] = 1.0 if k == self.state else 0.0

    # ------------------------------------------------------------ animation
    def update(self, dt, t):
        self.track_age += dt
        asleep = self.emotion == "sommeil"
        if self.carte is not None:
            self.carte_t += dt
            duree = float(self.carte.get("duree", CARTE_DUREE))
            if duree and self.carte_t > duree: self.set_carte(None)
        self.glance *= math.exp(-dt * 1.8)
        # le visage se range dans le tiers opposé à la carte, et reprend le centre ensuite
        tiers = (1 - CARTE_PART) / 2
        vise_cx = 0.5 if self.carte is None else (tiers if self.carte_cote() > 0 else 1 - tiers)
        vise_zoom = 1.0 if self.carte is None else CARTE_ZOOM
        k = 1 - math.exp(-dt * 5)
        self.cx += (vise_cx - self.cx) * k
        self.zoom += (vise_zoom - self.zoom) * k
        if self.track is not None and self.track_age < 3 and self.state != "thinking":
            gx, gy = self.track[0] * 0.9, self.track[1] * 0.6
        else:
            if t >= self.next_sacc and not asleep:
                self.sacc = (random.uniform(-0.25, 0.25), random.uniform(-0.15, 0.1))
                self.next_sacc = t + random.uniform(1.5, 4.0)
            gx, gy = (0.0, 0.0) if asleep or self.state == "thinking" else self.sacc
        if self.carte is not None and not asleep:   # coup d'œil appuyé à l'apparition, puis léger biais vers la carte
            gx += self.carte_cote() * (0.22 + 0.6 * self.glance)
        tgt = dict(self.target)
        tgt["look_x"] = max(-1, min(1, tgt["look_x"] + gx))
        tgt["look_y"] = max(-1, min(1, tgt["look_y"] + gy))
        a = 1 - math.exp(-dt * 8)
        for k, v in tgt.items():
            self.cur[k] += (v - self.cur[k]) * a
        self.level_s += (self.level - self.level_s) * (1 - math.exp(-dt * 18))
        if not self.talking: self.level *= math.exp(-dt * 6)
        # Le niveau d'écoute retombe seul en ~0,3 s : un datagramme perdu ne doit pas figer le trait en l'air.
        self.ecoute_age += dt
        if self.ecoute_age > 0.12: self.ecoute *= math.exp(-dt * 10)
        self.ecoute_s += (self.ecoute - self.ecoute_s) * (1 - math.exp(-dt * 18))
        for k in self.status_a:
            goal = 1.0 if k == self.state else 0.0
            self.status_a[k] += (goal - self.status_a[k]) * (1 - math.exp(-dt * 7))
        self.phase += dt * (6 + 10 * self.level_s)
        self.nuit_k += ((self.nuit_luminosite if self.nuit else 1.0) - self.nuit_k) * (1 - math.exp(-dt * 2))
        self.nuit_t += ((1.0 if self.nuit else 0.0) - self.nuit_t) * (1 - math.exp(-dt * 2))   # même lissage
        # Hors ligne : le visage ne savait rien d'une panne du cerveau. compagnon.py sortait, systemd le
        # relançait en boucle, et la TV continuait d'afficher une Bulle attentive qui n'écoutait plus rien.
        self.lien_age += dt
        self.hors_ligne = not self.sans_lien and self.lien_age > HORS_LIGNE_S
        self.vide += ((1.0 if self.hors_ligne else 0.0) - self.vide) * (1 - math.exp(-dt / 0.15))
        if self.nuit and self.state == "idle" and self.emotion != "sommeil" and self.status_a["speaking"] < 0.05:
            self.set_emotion("sommeil")
        if not asleep and self.blink_t < 0 and t >= self.next_blink:
            self.blink_t = 0.0
        if self.blink_t >= 0:
            self.blink_t += dt
            if self.blink_t > 0.16:
                self.blink_t = -1.0
                self.next_blink = t + (0.18 if random.random() < 0.15 else random.uniform(2.8, 6.5))

    def blink(self):
        if self.blink_t < 0: return 0.0
        return 1 - abs(self.blink_t / 0.16 * 2 - 1)

    # ------------------------------------------------------------ mise en page (pixels) → uniforms du shader
    def layout(self, t):
        """Calcule la géométrie de l'image courante ; le dessin lui-même est fait par le shader (bords exacts)."""
        K, p = self.K * self.zoom, self.cur                   # zoom/cx : le visage rétrécit et se range quand une carte s'affiche
        asleep = self.emotion == "sommeil"
        breath = math.sin(t * 2 * math.pi * (0.18 if asleep else 0.25))
        bounce = abs(math.sin(t * math.pi * 2.2)) * p["bounce"] * 5 * K
        speak_bob = self.level_s * 4 * K * self.status_a["speaking"]
        fx = self.W * self.cx + p["look_x"] * 70 * K          # les yeux entiers se tournent vers la personne…
        fy = self.H * 0.43 + p["look_y"] * 26 * K - bounce - speak_bob + breath * 1.5 * K
        bl = self.blink()
        # En sommeil, les paupières RESPIRENT (période 6 s, 0,55 → 1) au lieu de lâcher des « zzz » : ça dit
        # « en veille » sans faire dessin animé à côté du reste.
        souffle = (0.775 + 0.225 * math.sin(t * 2 * math.pi / 6.0)) if asleep else 1.0
        spread = (106 + p["spread"]) * K * (1 - 0.06 * abs(p["look_x"]))   # …et se rapprochent un peu (effet de rotation)
        m = 22 * K
        eyes, lids, ells, pups = [], [], [], []
        for side, cx, dy, sz, sm_extra in ((1, fx - spread, p["dy_l"], p["sz_l"], 0.0),
                                            (-1, fx + spread, p["dy_r"], p["sz_r"], p["smile_r"])):
            par = 1 + 0.07 * p["look_x"] * (-side)       # parallaxe : l'œil du côté regardé grossit un peu
            w = p["w"] * K * sz * par
            h = p["h"] * K * sz * par * (1 + 0.012 * breath)
            cy = fy + dy * K
            top = max(p["top"], bl * 0.97)
            smile = min(1.0, p["smile"] + sm_extra)
            yt = cy - h / 2 - m + (h * 0.98 + m) * top if top > 0.005 else -1e4
            slope = math.tan(math.radians(p["tilt"])) * side
            ew, eh = w * 1.9, h * 1.25
            ytop = cy + h / 2 + m * 0.2 - (h * 1.05) * smile
            ps = p["pupil"] * max(0.0, min(1.0, (0.3 - smile) / 0.15))
            pr = w * 0.30 * ps
            mx, my = max(0.0, w / 2 - pr - 5 * K), max(0.0, h / 2 - pr - 6 * K)
            eyes += [cx, cy, w, h]
            lids += [yt, slope, ytop, 1.0 if smile > 0.005 else 0.0]
            ells += [ew / 2, eh, p["bright"] * self.nuit_k * souffle, 0.0]
            pups += [cx + p["look_x"] * mx, cy + p["look_y"] * my, pr if ps > 0.05 else 0.0, max(1.5, pr * 0.24)]
        a = self.status_a
        # Un seul fil corail, qui change de forme selon l'état (board 1 du 21/09). Avant, trois signes sans
        # rapport : un trait, trois points, une onde.
        vif = self.ecoute_age < ECOUTE_REPLI_S            # un compagnon nous dit-il ce qu'il entend ?
        niveau = self.ecoute_s if vif else 0.0
        # Sans message d'écoute (visage en --demo, ancien compagnon), on garde la respiration d'avant : un
        # trait figé à sa longueur minimale se lirait comme une panne.
        intensite = (0.55 + 0.45 * niveau) if vif else (0.65 + 0.35 * math.sin(t * 3.2))
        # Le trait penche vers qui parle. C'est le regard suivi par la Kinect qui le dit, pas la direction des
        # micros : celle-ci n'est mesurée qu'APRÈS l'énoncé, trop tard pour animer quoi que ce soit.
        vers = self.track[0] if self.track is not None and self.track_age < 3 else 0.0
        # Réflexion : un point qui va et vient. s(3−s²)/2 pousse les valeurs vers ±1, donc il s'attarde aux
        # extrémités au lieu de balayer à vitesse constante.
        sp = math.sin(t * 2.2)
        return dict(
            u_eye=eyes, u_lid=lids, u_ell=ells, u_pup=pups,
            u_blush=[fx - spread - 14 * K, fx + spread + 14 * K, fy + p["h"] * K * 0.62, p["blush"] * self.nuit_k],
            u_st=[fx - p["look_x"] * 30 * K, self.H * 0.43 + 104 * K, K, t],
            u_sta=[a["listening"] * self.nuit_k * (1 - self.vide), a["thinking"] * self.nuit_k * (1 - self.vide),
                   a["speaking"] * self.nuit_k * (1 - self.vide),
                   intensite],
            # demi-longueur du trait d'écoute, son décalage vers qui parle, son inclinaison, et l'abscisse du
            # point de réflexion — tous en pixels, relatifs au centre du fil (u_st.xy)
            u_fil=[20 * K * (0.6 + 0.8 * niveau), vers * 20 * K, vers * 6 * K, sp * (3 - sp * sp) / 2 * 26 * K],
            u_wave=[120 * K, (2 + 16 * self.level_s) * K, self.phase, 0.0],
            # rapport à appliquer au blanc : (1, 1, 1) le jour, ambré la nuit. Le corail, lui, ne change pas
            # de teinte — seulement d'intensité, comme avant.
            u_teinte=[1 + (a / b - 1) * self.nuit_t for a, b in zip(AMBRE, BLANC_N)],
            u_vide=self.vide)


# ---------------------------------------------------------------- rendu GPU (OpenGL ES 2 / OpenGL 2)
VS = """
attribute vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
"""
FS = """
#ifdef GL_ES
precision highp float;
#endif
uniform vec2 u_res;
uniform vec4 u_eye[2]; uniform vec4 u_lid[2]; uniform vec4 u_ell[2]; uniform vec4 u_pup[2];
uniform vec4 u_blush; uniform vec4 u_st; uniform vec4 u_sta; uniform vec4 u_fil; uniform vec4 u_wave;
uniform vec3 u_teinte;                    // rapport appliqué au blanc : (1,1,1) le jour, ambré la nuit
uniform float u_vide;                     // 1 = hors ligne : les yeux ne sont plus qu'un contour
const vec3 WHITE = vec3(0.949, 0.933, 0.902);
const vec3 ACCENT = vec3(1.0, 0.416, 0.302);
// @reglages

float sdRoundBox(vec2 p, vec2 b, float r) { vec2 q = abs(p) - b + r; return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r; }
float sdSeg(vec2 p, vec2 a, vec2 b) { vec2 pa = p - a, ba = b - a; float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0); return length(pa - ba * h); }
float sdEllipse(vec2 p, vec2 r) { float k0 = length(p / r); float k1 = length(p / (r * r)); return k0 * (k0 - 1.0) / max(k1, 1e-4); }
float wave(float u) { return sin(u * 13.0 + u_wave.z) * 0.65 + sin(u * 23.0 - u_wave.z * 1.4) * 0.35; }
float shape(float d, float K) { return clamp(0.5 - d, 0.0, 1.0) + exp(-max(d, 0.0) / (4.0 * K)) * 0.35; }

void main() {
    vec2 p = vec2(gl_FragCoord.x, u_res.y - gl_FragCoord.y);
    float K = u_st.z;
    vec3 col = vec3(0.0);
    for (int i = 0; i < 2; i++) {
        vec4 e = u_eye[i]; vec4 l = u_lid[i]; vec4 el = u_ell[i]; vec4 pu = u_pup[i];
        vec2 q = p - e.xy;
        if (abs(q.x) > e.z * 0.5 + 45.0 * K || abs(q.y) > e.w * 0.5 + 45.0 * K) continue;
        float d = sdRoundBox(q, e.zw * 0.5, min(e.z, e.w) * 0.5);
        d = max(d, (l.x + l.y * q.x - p.y) / sqrt(1.0 + l.y * l.y));          // paupière haute
        if (l.w > 0.5) d = max(d, -sdEllipse(p - vec2(e.x, l.z + el.y), el.xy)); // yeux en croissant
        // Hors ligne : l'œil plein devient un contour de 2K. C'est le même œil, vidé — pas un autre dessin,
        // pour qu'on lise « elle n'est plus là » et pas « ce n'est plus elle ».
        d = mix(d, abs(d) - 1.0 * K, u_vide);
        float eye = clamp(0.5 - d, 0.0, 1.0);
        float glow = exp(-max(d, 0.0) / (HALO_ETENDUE * K)) * HALO_FORCE * (1.0 - eye) * (1.0 - u_vide);
        vec3 c = mix(WHITE * u_teinte * el.z, vec3(0.36 * el.z), u_vide);
        float pup = 0.0, hl = 0.0;
        if (pu.z > 0.5) {
            pup = clamp(0.5 - (length(p - pu.xy) - pu.z), 0.0, 1.0) * (1.0 - u_vide);
            hl = clamp(0.5 - (length(p - pu.xy - vec2(0.38, -0.4) * pu.z) - pu.w), 0.0, 1.0) * REFLET;
        }
        vec3 ec = mix(mix(c, vec3(0.0), pup), c, hl * pup);
        col += ec * eye + c * glow;
    }
    if (u_blush.w > 0.01) {
        for (int j = 0; j < 2; j++) {
            vec2 bq = (p - vec2(j == 0 ? u_blush.x : u_blush.y, u_blush.z)) / vec2(17.0 * K, 6.0 * K);
            col += ACCENT * exp(-dot(bq, bq) * 0.9) * 0.8 * u_blush.w;
        }
    }
    vec2 s = p - u_st.xy;
    if (abs(s.y) < 40.0 * K && abs(s.x) < 90.0 * K) {
        if (u_sta.x > 0.01) {                                       // écoute : le trait suit la voix entendue
            vec2 c = u_st.xy + vec2(u_fil.y, 0.0);                  // décalé vers qui parle
            float d = sdSeg(p, c - vec2(u_fil.x, -u_fil.z), c + vec2(u_fil.x, -u_fil.z)) - 1.6 * K;
            col += ACCENT * shape(d, K) * u_sta.x * u_sta.w;
        }
        if (u_sta.y > 0.01) {                                       // réflexion : un point qui va et vient
            float d = length(s - vec2(u_fil.w, 0.0)) - 4.5 * K;
            col += ACCENT * shape(d, K) * u_sta.y;
        }
        if (u_sta.z > 0.01) {                                       // parole : le fil de voix
            float L = u_wave.x; float u = (s.x + L * 0.5) / L;
            if (u > 0.0 && u < 1.0) {
                float env = pow(sin(3.14159 * u), 1.5);
                float y = wave(u) * u_wave.y * env;
                float u2 = min(u + 0.004, 0.999);
                float dy = (wave(u2) * u_wave.y * pow(sin(3.14159 * u2), 1.5) - y) / (0.004 * L);
                float d = abs(s.y - y) / sqrt(1.0 + dy * dy) - 1.5 * K;
                col += ACCENT * shape(d, K) * u_sta.z * smoothstep(0.0, 0.05, u) * smoothstep(1.0, 0.95, u);
            }
        }
    }
    gl_FragColor = vec4(min(col, vec3(1.0)), 1.0);
}
""".replace("// @reglages", f"""const float HALO_ETENDUE = {HALO_ETENDUE};
const float HALO_FORCE = {HALO_FORCE};
const float REFLET = {REFLET};""")


CARTE_VS = """
attribute vec2 a_pos;
uniform vec4 u_quad;                      // centre x, y et demi-taille en coordonnées écran normalisées
varying vec2 v_uv;
void main() {
    v_uv = vec2((a_pos.x + 1.0) * 0.5, (1.0 - a_pos.y) * 0.5);
    gl_Position = vec4(u_quad.x + a_pos.x * u_quad.z, u_quad.y + a_pos.y * u_quad.w, 0.0, 1.0);
}
"""
CARTE_FS = """
#ifdef GL_ES
precision mediump float;
#endif
uniform sampler2D u_tex;
uniform vec3 u_carte;                     // x = fraction de temps restante (<0 : pas de filet),
uniform vec4 u_filet;                     // y = luminosité (mode nuit), z = fondu
uniform vec3 u_teinte;                    // u_filet = le filet de tête en uv : x, y, largeur, hauteur
varying vec2 v_uv;                        // u_teinte = rapport appliqué au blanc, ambré la nuit
const vec3 ACCENT = vec3(1.0, 0.416, 0.302);
void main() {
    vec4 c = texture2D(u_tex, v_uv);
    // La carte suit le visage : à 3 h du matin, un texte blanc franc à côté d'un visage ambré fait écran.
    vec3 col = c.rgb * u_carte.y * u_teinte;
    float a = c.a;
    // Le filet de l'étiquette EST le compte à rebours : il rétrécit de la gauche vers la droite pendant la
    // durée de la carte. La barre de 6 px du bas a disparu avec lui — deux signes pour une seule information.
    if (u_carte.x >= 0.0 && v_uv.x >= u_filet.x && v_uv.x < u_filet.x + u_filet.z * u_carte.x
        && v_uv.y >= u_filet.y && v_uv.y < u_filet.y + u_filet.w) {
        col = ACCENT * u_carte.y;
        a = 1.0;
    }
    gl_FragColor = vec4(col, a * u_carte.z);
}
"""


class GLRenderer:
    def __init__(self, W, H):
        from OpenGL import GL as gl
        import array
        self.gl, self.W, self.H = gl, W, H

        def sh(kind, src):
            s = gl.glCreateShader(kind); gl.glShaderSource(s, src); gl.glCompileShader(s)
            if not gl.glGetShaderiv(s, gl.GL_COMPILE_STATUS): raise RuntimeError(gl.glGetShaderInfoLog(s))
            return s

        def prog(vs, fs, noms):
            p = gl.glCreateProgram()
            gl.glAttachShader(p, sh(gl.GL_VERTEX_SHADER, vs)); gl.glAttachShader(p, sh(gl.GL_FRAGMENT_SHADER, fs))
            gl.glBindAttribLocation(p, 0, "a_pos"); gl.glLinkProgram(p)
            if not gl.glGetProgramiv(p, gl.GL_LINK_STATUS): raise RuntimeError(gl.glGetProgramInfoLog(p))
            return p, {n: gl.glGetUniformLocation(p, n) for n in noms}
        self.prog, self.loc = prog(VS, FS, ("u_res", "u_eye", "u_lid", "u_ell", "u_pup", "u_blush", "u_st", "u_sta",
                                            "u_fil", "u_wave", "u_teinte", "u_vide"))
        self.cprog, self.cloc = prog(CARTE_VS, CARTE_FS, ("u_quad", "u_carte", "u_filet", "u_teinte", "u_tex"))
        vbo = gl.glGenBuffers(1); gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, array.array("f", [-1, -1, 1, -1, -1, 1, 1, 1]).tobytes(), gl.GL_STATIC_DRAW)
        gl.glEnableVertexAttribArray(0); gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, False, 0, None)
        gl.glEnable(gl.GL_BLEND); gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glUseProgram(self.prog)
        gl.glViewport(0, 0, W, H); gl.glUniform2f(self.loc["u_res"], W, H)
        self.tex, self.tex_sz, self.filet = None, (0, 0), None

    def draw(self, u):
        gl, L = self.gl, self.loc
        gl.glUseProgram(self.prog)
        for n in ("u_eye", "u_lid", "u_ell", "u_pup"): gl.glUniform4fv(L[n], 2, u[n])

        for n in ("u_blush", "u_st", "u_sta", "u_fil", "u_wave"): gl.glUniform4f(L[n], *u[n])
        gl.glUniform3f(L["u_teinte"], *u["u_teinte"]); gl.glUniform1f(L["u_vide"], u["u_vide"])
        gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)

    def set_carte(self, surf, filet=None):
        """Téléverse la carte (surface pygame RVBA) dans une texture ; None = plus de carte.

        `filet` est le rectangle du trait de tête en pixels de la carte (carte.rendre). Il reste HORS de la
        texture : il rétrécit à chaque image, la texture ne se téléverse qu'une fois.
        """
        gl = self.gl
        if surf is None:
            self.tex_sz, self.filet = (0, 0), None; return
        if self.tex is None:
            self.tex = gl.glGenTextures(1)
        w, h = surf.get_size()
        octets = getattr(pygame.image, "tobytes", None) or pygame.image.tostring
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        # GLES 2 : pas de mipmap ni de répétition sur une texture de taille quelconque
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, octets(surf, "RGBA", False))
        self.tex_sz = (w, h)
        self.filet = tuple(v / (w if i % 2 == 0 else h) for i, v in enumerate(filet)) if filet else None

    def draw_carte(self, x0, alpha, reste, nuit_k, teinte=(1.0, 1.0, 1.0)):
        """x0 = bord gauche de la carte en pixels ; reste = fraction de temps restante (<0 : pas de filet)."""
        gl, L = self.gl, self.cloc
        if not self.tex_sz[0] or alpha <= 0.003: return
        w = self.tex_sz[0]
        xg = 2.0 * x0 / self.W - 1.0
        gl.glUseProgram(self.cprog)
        gl.glActiveTexture(gl.GL_TEXTURE0); gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glUniform1i(L["u_tex"], 0)
        gl.glUniform4f(L["u_quad"], xg + w / self.W, 0.0, w / self.W, 1.0)
        gl.glUniform3f(L["u_carte"], reste if self.filet else -1.0, nuit_k, alpha)
        gl.glUniform4f(L["u_filet"], *(self.filet or (0.0, 0.0, 0.0, 0.0)))
        gl.glUniform3f(L["u_teinte"], *teinte)
        gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)


def open_display(res, window, hidden=False):
    """Contexte OpenGL : ES 2 sur le Pi (KMS), OpenGL classique ailleurs."""
    kms = os.environ.get("SDL_VIDEODRIVER") == "kmsdrm"
    pygame.display.init(); pygame.font.init()
    if kms:
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_ES)
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 2)
    flags = pygame.OPENGL | pygame.DOUBLEBUF | (pygame.HIDDEN if hidden else 0) | (0 if window else pygame.FULLSCREEN)
    size = (0, 0) if res == "native" else tuple(map(int, res.split("x")))
    screen = pygame.display.set_mode(size, flags, vsync=1)
    return screen.get_size()


# ---------------------------------------------------------------- programme
def render_grid(path, W=640, H=360):
    from OpenGL import GL as gl
    open_display(f"{W}x{H}", window=True, hidden=True)
    face, r = Face(W, H), GLRenderer(W, H)
    face.sans_lien = True                     # la planche n'a pas de compagnon : elle montre le visage, pas la panne
    cells = [(e, "idle") for e in EMOTIONS] + [("neutre", "listening"), ("neutre", "thinking"), ("joie", "speaking")]
    cw, ch, cols = 320, 206, 4
    rows = math.ceil(len(cells) / cols)
    sheet = pygame.Surface((cw * cols, ch * rows)); sheet.fill((236, 236, 232))
    lab = pygame.font.Font(None, 24)
    for i, (e, st) in enumerate(cells):
        face.set_emotion(e); face.set_state(st); face.level = face.level_s = 0.55 if st == "speaking" else 0
        face.phase = 1.3; face.snap(); face.blink_t = -1
        r.draw(face.layout(0.9 if st != "thinking" else 0.45))
        buf = gl.glReadPixels(0, 0, W, H, gl.GL_RGB, gl.GL_UNSIGNED_BYTE)
        frame = pygame.transform.flip(pygame.image.frombuffer(bytes(buf), (W, H), "RGB"), False, True)
        cell = pygame.transform.smoothscale(frame, (cw - 12, (cw - 12) * H // W))
        x, y = (i % cols) * cw + 6, (i // cols) * ch + 4
        sheet.blit(cell, (x, y))
        txt = lab.render(e if st == "idle" else f"état : {st}", True, (30, 30, 30))
        sheet.blit(txt, (x + (cw - 12 - txt.get_width()) // 2, y + cell.get_height() + 5))
    pygame.image.save(sheet, path)
    print("planche :", path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", help="rend les émotions et états dans une image et quitte")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--res", default="1280x720", help="définition de rendu (« native » = celle de l'écran)")
    ap.add_argument("--window", action="store_true", help="fenêtré (sinon plein écran)")
    ap.add_argument("--sans-lien", action="store_true",
                    help="ne jamais afficher l'écran « hors ligne » (visage lancé seul, sans compagnon)")
    ap.add_argument("--demo", action="store_true", help="défilement automatique")
    ap.add_argument("--stats", type=float, default=0, help="affiche les images/s toutes les N secondes")
    a = ap.parse_args()
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")  # le visage ne joue aucun son : laisse la sortie HDMI au client
    if os.environ.get("SDL_VIDEODRIVER") == "kmsdrm":
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    if a.grid:
        render_grid(a.grid); return
    W, H = open_display(a.res, a.window)
    pygame.display.set_caption("Bulle")
    pygame.mouse.set_visible(False)
    face, renderer = Face(W, H), GLRenderer(W, H)
    # En démo comme avec --sans-lien, personne n'envoie de battement : sans ça le visage se déclarerait en
    # panne au bout de vingt secondes de démonstration.
    face.sans_lien = a.sans_lien or a.demo
    carte.prechauffer(H)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", a.port)); sock.setblocking(False)
    clock, t, demo, demo_i, demo_next = pygame.time.Clock(), 0.0, a.demo, 0, 0.0
    demo_seq = [(e, "idle") for e in EMOTIONS] + [("neutre", "listening"), ("neutre", "thinking"), ("joie", "speaking")]
    gaze, fake_talk, stats_next = [0.0, 0.0], False, 0.0
    keys_state = {pygame.K_l: "listening", pygame.K_t: "thinking", pygame.K_s: "speaking", pygame.K_i: "idle"}
    carte_etat, demo_carte = (None, -1), 0        # (signature de la carte, version des images) déjà téléversée
    while True:
        dt = clock.tick(30) / 1000; t += dt
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                pygame.quit(); return
            if ev.type == pygame.KEYDOWN:
                if ev.unicode and ev.unicode.isdigit() and 1 <= int(ev.unicode) <= len(EMOTIONS):
                    face.set_emotion(EMOTIONS[int(ev.unicode) - 1]); demo = False
                elif ev.key in keys_state: face.set_state(keys_state[ev.key]); demo = False
                elif ev.key == pygame.K_c:            # contrôle de lisibilité des cartes, sans le cerveau
                    face.set_carte(None if demo_carte >= len(carte.EXEMPLES) else carte.EXEMPLES[demo_carte])
                    demo_carte = 0 if demo_carte >= len(carte.EXEMPLES) else demo_carte + 1
                elif ev.key == pygame.K_SPACE:
                    demo = not demo; demo_next = t
                    face.sans_lien = a.sans_lien or demo
                elif ev.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN):
                    d = {pygame.K_LEFT: (-0.35, 0), pygame.K_RIGHT: (0.35, 0), pygame.K_UP: (0, -0.35), pygame.K_DOWN: (0, 0.35)}[ev.key]
                    gaze = [max(-1, min(1, gaze[0] + d[0])), max(-1, min(1, gaze[1] + d[1]))]
                    face.command({"gaze": gaze})
        if pygame.key.get_pressed()[pygame.K_m]:
            face.command({"talking": True, "mouth": abs(math.sin(t * 11) * math.sin(t * 3.7)) * random.uniform(0.6, 1)})
            fake_talk = True
        elif fake_talk:
            face.command({"talking": False}); fake_talk = False
        while True:
            try: data, _ = sock.recvfrom(8192)
            except (BlockingIOError, OSError): break
            try: face.command(json.loads(data))
            except (ValueError, TypeError, KeyError): pass
        if demo and t >= demo_next:
            e, st = demo_seq[demo_i % len(demo_seq)]; demo_i += 1
            face.set_emotion(e); face.set_state(st); face.talking = st == "speaking"; demo_next = t + 2.5
        if demo and face.talking: face.level = abs(math.sin(t * 11) * math.sin(t * 3.7))
        face.update(dt, t)
        if carte_etat != (face.carte_sig, carte.VERSION):      # nouvelle carte, ou pochette qui vient d'arriver
            carte_etat = (face.carte_sig, carte.VERSION)
            largeur = int(W * CARTE_PART)
            try:
                renderer.set_carte(*(carte.rendre(face.carte, largeur, H) if face.carte else (None, None)))
            except Exception as e:
                # Une carte mal formée ne doit JAMAIS emporter le visage : il est l'affichage du salon, et il
                # redémarrait sous les yeux de Greg dès qu'une image d'un format inattendu arrivait (21/09).
                print("carte non dessinée :", type(e).__name__, e, flush=True)
                renderer.set_carte(None)
        u = face.layout(t)
        renderer.draw(u)
        if face.carte:
            duree = float(face.carte.get("duree", CARTE_DUREE))
            # duree 0 (la musique, qui reste tant que ça joue) : filet plein et fixe. Il n'y a rien à décompter,
            # mais la carte garde son entrée en matière.
            reste = max(0.0, 1 - face.carte_t / duree) if duree else 1.0
            renderer.draw_carte(0 if face.carte_cote() < 0 else W - int(W * CARTE_PART),
                                face.carte_alpha(), reste, face.nuit_k, u["u_teinte"])
        pygame.display.flip()
        if a.stats and t >= stats_next:
            print(f"{clock.get_fps():.1f} ips ({face.emotion}/{face.state})", flush=True); stats_next = t + a.stats


if __name__ == "__main__":
    main()
