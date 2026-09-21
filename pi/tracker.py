"""Suivi de la personne par la profondeur Kinect (sans squelette) → regard du visage + présence pour le client.

  - fond = profondeur la plus lointaine vue par case (se met à jour quand la pièce se dévoile) ;
  - premier plan = cases nettement plus proches que le fond ; la plus grande silhouette = la personne ;
  - envoie {"gaze": [x, y]} au visage (UDP 5005, ~12 Hz) et {"presence": bool} au client (UDP 5006).
"""
import argparse, ctypes, ctypes.util, json, math, os, socket, time

import numpy as np

lib = ctypes.CDLL(os.environ.get("FREENECT_SYNC") or ctypes.util.find_library("freenect_sync") or "libfreenect_sync.so.0")
lib.freenect_sync_get_depth.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32), ctypes.c_int, ctypes.c_int]
DEPTH_MM = 5
GW, GH = 80, 60                 # grille de travail (640×480 / 8)
FOV_X, FOV_Y = 57.0, 43.0       # champ de vision de la Kinect 1 (degrés)
REGLES = os.environ.get("BULLE_REGLES", os.path.expanduser("~/kinectface/regles.yaml"))
ETAT = os.path.expanduser("~/kinectface/etat/suivi.json")


def regle(cle, defaut):
    """Réglages lus dans regles.yaml (section « suivi »), avec valeur de repli."""
    try:
        import yaml
        return (yaml.safe_load(open(REGLES, encoding="utf-8")).get("suivi") or {}).get(cle, defaut)
    except Exception:
        return defaut


MIN_SPAN = regle("hauteur_min_lignes", 12)       # une silhouette doit être haute (objet au sol ignoré)
IMMOBILE_S = regle("immobile_absorbe_s", 180)    # une « personne » figée aussi longtemps est du mobilier : dans le fond
IMMOBILE_X, IMMOBILE_D = 1.2, 0.15               # tolérances de l'immobilité : cases de grille, et mètres
SEEN_ON = regle("presence_mesures", 6)           # mesures d'affilée pour annoncer une présence
LOST_OFF = regle("absence_s", 6.0)               # secondes sans personne avant d'annoncer un départ
FLICK_MAX = regle("scintillement_max", 0.25)
BORD_G, BORD_D = regle("bord_gauche", 4), regle("bord_droit", 5)


class Raw(ctypes.Structure):
    _fields_ = [("accel_x", ctypes.c_int16), ("accel_y", ctypes.c_int16), ("accel_z", ctypes.c_int16),
                ("tilt_angle", ctypes.c_int8), ("tilt_status", ctypes.c_int)]


lib.freenect_sync_get_tilt_state.argtypes = [ctypes.POINTER(ctypes.POINTER(Raw)), ctypes.c_int]
lib.freenect_sync_set_tilt_degs.argtypes = [ctypes.c_int, ctypes.c_int]


class Thermique:
    """Relève la température et le bridage du Pi, et en garde le MAXIMUM sur 24 h.

    Le bilan de santé n'était pris qu'à 3 h du matin, par la boucle de nuit : le Pi y est froid depuis des
    heures et `get_throttled` y vaut 0x20000, que l'analyste tolère à juste titre (ce bit est collant depuis le
    démarrage). En journée il vaut 0x20002 — bridé MAINTENANT — et personne ne le voyait. D'où : on échantillonne
    ici, dans la seule boucle qui tourne en continu sur le Pi, et on publie le pire, pas l'instantané.

    Un redémarrage du suivi repart de zéro : `fenetre_h` dit sur combien de temps porte le maximum, pour qu'on
    ne prenne pas un relevé de deux minutes pour une journée calme.
    """

    FENETRE = 24 * 3600

    def __init__(self):
        self.mesures = []          # (ts, °C, bridé) — une toutes les 30 s, soit ~2 880 sur 24 h

    @staticmethod
    def _temperature():
        # sysfs plutôt que « vcgencmd measure_temp » : une lecture de fichier au lieu d'un processus, 2 880 fois par jour
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return round(int(f.read()) / 1000, 1)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _throttled():
        try:
            import subprocess
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
            return out.strip().split("=")[-1] or None
        except Exception:
            return None

    @staticmethod
    def _bride(mot):
        """Vrai si la fréquence est bridée EN CE MOMENT.

        Les bits hauts (0x10000 et au-dessus) sont collants depuis le démarrage : ils disent que c'est déjà
        arrivé, pas que ça arrive. Seuls les quatre bits bas parlent du présent.
        """
        try:
            return bool(int(mot, 16) & 0xF)
        except (TypeError, ValueError):
            return False          # ce relevé vit dans la boucle temps réel du suivi : il n'a pas le droit de lever

    def releve(self, now):
        t, mot = self._temperature(), self._throttled()
        bride = self._bride(mot)
        if t is not None:
            self.mesures.append((now, t, bride))
        limite = now - self.FENETRE
        while self.mesures and self.mesures[0][0] < limite:
            self.mesures.pop(0)
        return mot

    def bilan(self, now, mot):
        if not self.mesures:
            return {"throttled": mot}
        temps = [m[1] for m in self.mesures]
        brides = sum(1 for m in self.mesures if m[2])
        chaud = max(self.mesures, key=lambda m: m[1])
        return {"temperature_c": temps[-1], "temperature_max_c": chaud[1],
                "temperature_max_il_y_a_min": round((now - chaud[0]) / 60),
                "bride_maintenant": self.mesures[-1][2],
                "bride_part": round(brides / len(self.mesures), 3),
                "fenetre_h": round((now - self.mesures[0][0]) / 3600, 1), "throttled": mot}


def inclinaison():
    """Angle réel de la Kinect (accéléromètre), en degrés : + = vers le haut."""
    st = ctypes.POINTER(Raw)()
    if lib.freenect_sync_get_tilt_state(ctypes.byref(st), 0) != 0:
        return None
    s = st.contents
    return round(math.degrees(math.atan2(s.accel_z, s.accel_y)), 1)


def depth_grid():
    ptr, ts = ctypes.c_void_p(), ctypes.c_uint32()
    if lib.freenect_sync_get_depth(ctypes.byref(ptr), ctypes.byref(ts), 0, DEPTH_MM) != 0:
        return None
    d = np.frombuffer((ctypes.c_uint16 * (640 * 480)).from_address(ptr.value), np.uint16).reshape(480, 640)
    # 4 échantillons par case 8×8 (au lieu de 64) : 16 fois moins de calcul, assez pour une silhouette
    b = d[2::8, 2::8], d[2::8, 6::8], d[6::8, 2::8], d[6::8, 6::8]
    st = np.stack(b).astype(np.float32)
    valid = (st > 0).sum(axis=0)
    g = np.where(valid >= 2, st.sum(axis=0) / np.maximum(valid, 1), 0) / 1000.0   # mètres, 0 = inconnu
    return g


def find_person(fg, g, prev_x=None):
    """Bandes de colonnes occupées ; on garde la plus massive, en favorisant celle proche de la position précédente."""
    cols = fg.sum(axis=0)
    occ = cols >= 3
    runs, cur = [], None
    for x in range(GW + 1):
        if x < GW and occ[x]:
            cur = [x, x] if cur is None else [cur[0], x]
        elif cur is not None:
            runs.append((cur[0], cur[1], int(cols[cur[0]:cur[1] + 1].sum()))); cur = None
    runs = [r for r in runs if r[2] >= 25]      # trop petit : bruit
    if not runs:
        return None
    def score(r):
        if prev_x is None: return r[2]
        return r[2] - 6 * abs((r[0] + r[1]) / 2 - prev_x)
    x0, x1, _ = max(runs, key=score)
    sub = fg[:, x0:x1 + 1]
    xs = np.arange(x0, x1 + 1)
    cx = float((sub.sum(axis=0) * xs).sum() / sub.sum())
    rows = np.where(sub.any(axis=1))[0]
    top = int(rows.min())
    # une personne est haute : un objet au sol (quelques lignes en bas de l'image) n'en est pas une
    if rows.max() - top < MIN_SPAN or top > GH * 0.6:
        return None
    head = sub[top:top + 6]                     # le haut de la silhouette ≈ la tête
    hx = float((head.sum(axis=0) * xs).sum() / max(1, head.sum())) if head.any() else cx
    dist = float(np.median(g[:, x0:x1 + 1][sub]))
    return hx, top, dist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--face", default="127.0.0.1:5005")
    ap.add_argument("--client", default="127.0.0.1:5006")
    ap.add_argument("--hz", type=float, default=8)
    ap.add_argument("--flip", action="store_true", help="inverse gauche/droite du regard")
    ap.add_argument("--min-dist", type=float, default=0.6)
    ap.add_argument("--max-dist", type=float, default=regle("distance_max_m", 4.5))
    ap.add_argument("--gap", type=float, default=regle("ecart_fond_m", 0.25), help="écart au fond (m) pour être au premier plan")
    ap.add_argument("--commande", type=int, default=5007, help="port UDP local des commandes ({\"inclinaison\": degrés})")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    face = (a.face.split(":")[0], int(a.face.split(":")[1]))
    client = (a.client.split(":")[0], int(a.client.split(":")[1]))
    bg, prev, present, seen, lost_since, last_log, track_x = None, None, False, 0, 0.0, 0.0, None
    flick = np.zeros((GH, GW), np.float32)   # fréquence de scintillement par case (vitre, reflet : permanente)
    gx_s = gy_s = 0.0
    period = 1.0 / a.hz
    cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd.bind(("127.0.0.1", a.commande)); cmd.setblocking(False)
    os.makedirs(os.path.dirname(ETAT), exist_ok=True)
    n_img, n_pers, bascules, t_bilan = 0, 0, 0, time.time()
    fige_depuis, fige_ref = None, None           # silhouette qui ne bouge pas : candidate à l'absorption
    thermique = Thermique()
    while True:
        t0 = time.time()
        g = depth_grid()
        if g is None:
            time.sleep(1); continue
        known = g > 0
        if bg is None:
            bg = g.copy()
        # le fond garde la profondeur la plus lointaine observée (la pièce « se dévoile » quand on bouge),
        # mais seulement si la mesure est stable d'une image à l'autre (une vitre ou un reflet scintille)
        stable = known if prev is None else known & (prev > 0) & (np.abs(g - prev) < 0.08)
        if prev is not None:
            jump = (known != (prev > 0)) | (known & (prev > 0) & (np.abs(g - prev) > 0.15))
            flick = 0.97 * flick + 0.03 * jump
        bg = np.where(stable & (g > bg), g, bg)
        prev = g
        fg = known & (bg - g > a.gap) & (g > a.min_dist) & (g < a.max_dist)
        # nettoyage : une case isolée n'est pas une personne
        fg = fg & (np.roll(fg, 1, 0) | np.roll(fg, -1, 0)) & (np.roll(fg, 1, 1) | np.roll(fg, -1, 1))
        fg[:, :BORD_G] = False; fg[:, GW - BORD_D:] = False   # bords de l'image : peu fiables
        fg &= flick < FLICK_MAX                   # cases qui scintillent en permanence : jamais une personne
        p = find_person(fg, g, track_x)
        # Une personne bouge, même assise. Une silhouette parfaitement figée est un meuble déplacé, un sac, une
        # plante : sans ça, le suivi la « voit » indéfiniment, le regard reste collé dessus et la vraie personne
        # n'est plus jamais élue (vu le 21/09 : part_avec_personne = 1,0 et zéro bascule pendant des heures).
        if p:
            if fige_ref and abs(p[0] - fige_ref[0]) < IMMOBILE_X and abs(p[2] - fige_ref[2]) < IMMOBILE_D:
                if time.time() - fige_depuis > IMMOBILE_S:
                    bg = np.where(fg, g, bg)     # le fond apprend le meuble : il cesse d'être au premier plan
                    print(f"silhouette immobile depuis {IMMOBILE_S:.0f} s absorbée dans le fond "
                          f"(angle {((p[0] + 0.5) / GW - 0.5) * FOV_X:+.0f}°, {p[2]:.1f} m)", flush=True)
                    fige_depuis, fige_ref, p, track_x = None, None, None, None
            else:
                fige_depuis, fige_ref = time.time(), p
        else:
            fige_depuis, fige_ref = None, None
        now = time.time()
        n_img += 1; n_pers += bool(p)
        try:                                      # commande locale : {"inclinaison": degrés}
            d = json.loads(cmd.recv(1024))
            if "inclinaison" in d:
                v = max(-27, min(27, int(d["inclinaison"])))
                lib.freenect_sync_set_tilt_degs(v, 0)
                print(f"inclinaison demandée : {v}°", flush=True)
                bg = None; flick[:] = 0          # la scène change : on réapprend le fond
                continue
        except (BlockingIOError, ValueError, OSError):
            pass
        if now - t_bilan > 30:                    # bilan de santé pour l'agent (etat/suivi.json)
            haut = g[: GH // 3]
            mot = thermique.releve(now)
            bilan = {"ts": round(now), "inclinaison_deg": inclinaison(), "profondeur_valide": round(float(known.mean()), 3),
                     "profondeur_valide_haut": round(float((haut > 0).mean()), 3),
                     "distance_mediane_m": round(float(np.median(g[known])), 2) if known.any() else None,
                     "cases_scintillantes": int((flick >= FLICK_MAX).sum()), "images_par_s": round(n_img / (now - t_bilan), 1),
                     "personne_angle_deg": round(((p[0] + 0.5) / GW - 0.5) * FOV_X, 1) if p else None,
                     "personne_distance_m": round(p[2], 2) if p else None,
                     "immobile_depuis_s": round(now - fige_depuis) if fige_depuis else 0,
                     "part_avec_personne": round(n_pers / max(1, n_img), 2), "bascules_presence": bascules, "present": present,
                     **thermique.bilan(now, mot)}
            with open(ETAT + ".tmp", "w") as f: json.dump(bilan, f)
            os.replace(ETAT + ".tmp", ETAT)
            n_img, n_pers, bascules, t_bilan = 0, 0, 0, now
        if p:
            hx, top, dist = p
            seen += 1; lost_since = 0.0; track_x = hx
            ax = ((hx + 0.5) / GW - 0.5) * FOV_X                 # angle horizontal (°), + = droite de l'image
            ay = ((top + 3) / GH - 0.5) * FOV_Y                  # angle vertical de la tête (°), + = bas
            gx = max(-1.0, min(1.0, -ax / 25.0))                 # vu depuis l'écran : la gauche de l'image = droite de la TV
            if a.flip: gx = -gx
            gy = max(-1.0, min(1.0, ay / 20.0))
            gx_s += (gx - gx_s) * 0.35; gy_s += (gy - gy_s) * 0.35
            if seen >= SEEN_ON:
                sock.sendto(json.dumps({"gaze": [round(gx_s, 3), round(gy_s, 3)]}).encode(), face)
                if seen % 8 == 0:   # ~1/s : angle pour le journal du client (comparaison avec l'orientation par la voix)
                    sock.sendto(json.dumps({"angle": round(ax, 1)}).encode(), client)
                if not present:
                    present = True; bascules += 1
                    sock.sendto(json.dumps({"presence": True, "dist": round(dist, 2)}).encode(), client)
            if a.verbose and now - last_log > 1:
                print(f"personne : angle {ax:+5.1f}°  tête {ay:+5.1f}°  distance {dist:.2f} m  regard {gx_s:+.2f},{gy_s:+.2f}", flush=True)
                last_log = now
        else:
            seen = 0; track_x = None if not present else track_x
            if present:
                lost_since = lost_since or now
                if now - lost_since > LOST_OFF:
                    present = False; bascules += 1
                    sock.sendto(json.dumps({"gaze": None}).encode(), face)
                    sock.sendto(json.dumps({"presence": False}).encode(), client)
                    if a.verbose: print("plus personne", flush=True)
        time.sleep(max(0.0, period - (time.time() - t0)))


if __name__ == "__main__":
    main()
