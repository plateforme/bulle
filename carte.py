"""Cartes de Bulle — ce que la voix dit mal s'affiche à côté du visage.

Principe : la voix reste autosuffisante (depuis la cuisine, on ne rate rien) ; la carte ne fait que préciser.
Elle est donc courte, lisible à 4 m, et éphémère. Le visage se décale dans le tiers opposé et la regarde.

Le cerveau envoie des DONNÉES, pas une image (cerveau/cartes.py) — c'est ce qui permettra de remettre en page
selon la distance mesurée par la Kinect sans rien redemander au réseau.

    {"gabarit": "liste",  "titre": "Courses", "items": [{"cle": "1", "texte": "Lait d'avoine"}, …]}
    {"gabarit": "cles",   "titre": "Montréal", "items": [{"cle": "demain", "valeur": "-4°", "note": "neige"}, …]}
    {"gabarit": "media",  "texte": "Digital Love", "note": "Daft Punk", "accent": "CXN100", "image": "https://…"}
    {"gabarit": "lieu",   "texte": "Place des Arts", "note": "175 rue Sainte-Catherine O, Montréal",
     "accent": "à 3,2 km au nord-est", "image": "http://…/plan/xxx.png"}
    {"gabarit": "texte",  "titre": "Ça s'écrit", "texte": "Kyutai", "epeler": true, "note": "kyutai.org"}
  communs : "duree" (s, 0 = jusqu'au remplacement), "cote" ("droite" par défaut : le visage part à gauche)
"""
import io, os, re, threading, urllib.request

import pygame

BLANC = (242, 238, 230)
ACCENT = (255, 106, 77)
# Le corail ne dit plus qu'une chose : ce que fait Bulle (le compte à rebours de la carte, le prochain
# événement). Partout ailleurs il criait sans rien signifier — clés, notes, épellation (revue du 21/09).
GRIS_CLAIR = (154, 150, 143)      # étiquette, clés, notes : gris CHAUD, le gris neutre salissait le blanc
BLANC_ATTENUE = (207, 202, 193)   # la note sous une grosse valeur : deuxième rang, mais encore du texte
SOURD = (74, 74, 74)              # les éléments passés, et eux seuls (contraste ≈ 2,4:1 : illisible exprès)

def _jeton():
    """Même secret que le compagnon ; le visage tourne dans un autre processus, il le relit."""
    j = os.environ.get("BULLE_JETON", "").strip()
    if j: return j
    try:
        with open(os.path.expanduser("~/.config/bulle/jeton"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


_POLICES, _CHEMIN, _GRAISSES = {}, None, {}
IMAGES = {}      # url → Surface, ou False si le téléchargement a échoué
VERSION = 0      # incrémenté quand une image arrive : le visage sait qu'il doit refaire la carte
# Chemins connus d'abord : sur le Pi, pygame.font.match_font énumère TOUTES les polices du système
# (~1,5 s la première fois, soit une trentaine d'images perdues au moment où la carte apparaît).
CHEMINS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
           "C:/Windows/Fonts/segoeui.ttf")
# Geist (OFL) en quatre graisses : la hiérarchie d'une carte reposait sur la seule taille et la seule couleur,
# d'où l'aspect « terminal » relevé le 21/09. Pas d'italique ni de gras : ça ne se lit pas à 3,5 m.
GRAISSES = {"extralight": "Geist-ExtraLight.ttf", "light": "Geist-Light.ttf",
            "regular": "Geist-Regular.ttf", "medium": "Geist-Medium.ttf"}
# Sur le Pi, deployer_pi.sh dépose les polices dans ~/kinectface/polices ; sur le PC et en CI, elles sont dans
# le dépôt, à côté de ce fichier. Un test masque ces deux dossiers pour vérifier le repli.
DOSSIERS = (os.path.expanduser("~/kinectface/polices"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi", "polices"))


def _repli():
    """La police du système, quand Geist n'est pas là. Résolue une seule fois (match_font coûte ~1,5 s)."""
    global _CHEMIN
    if _CHEMIN is None:
        _CHEMIN = next((c for c in CHEMINS if os.path.exists(c)), None) or \
                  pygame.font.match_font("dejavusans,freesans,liberationsans,arial") or ""
    return _CHEMIN


def _fichier(graisse):
    """Le Geist de cette graisse, ou le repli s'il manque.

    Le repli n'est pas négociable : une police absente doit coûter un aspect, jamais une carte — le visage est
    l'affichage du salon (même raison que le try/except de face.main, 21/09). Un déploiement à moitié fait, une
    carte SD reflashée, et Bulle continue d'afficher en DejaVu.
    """
    if graisse not in _GRAISSES:
        nom = GRAISSES.get(graisse) or GRAISSES["regular"]
        _GRAISSES[graisse] = next((os.path.join(d, nom) for d in DOSSIERS
                                   if os.path.exists(os.path.join(d, nom))), None) or _repli()
    return _GRAISSES[graisse]


def police(px, graisse="regular"):
    px = max(11, int(px))
    if (px, graisse) not in _POLICES:
        if not pygame.font.get_init(): pygame.font.init()
        chemin = _fichier(graisse)
        _POLICES[(px, graisse)] = pygame.font.Font(chemin, px) if chemin else pygame.font.Font(None, px)
    return _POLICES[(px, graisse)]


# Les couples (taille, graisse) que rendre() demande vraiment. Préchauffer la seule taille, comme avant les
# graisses, reperdrait une trentaine d'images à la première carte : chaque couple est un fichier à ouvrir.
PRECHAUFFE = ((22, "medium"),                                                          # étiquette de tête
              (24, "light"), (26, "light"), (30, "light"), (32, "light"),              # clés, notes, pastille
              (32, "regular"), (42, "regular"), (44, "regular"), (56, "regular"), (58, "regular"), (76, "regular"),
              (42, "light"), (58, "light"),                                            # la clé suit le corps
              (25.6, "regular"), (33.6, "regular"), (46.4, "regular"),                 # cartes `cles` en lignes
              (25.6, "light"), (33.6, "light"), (46.4, "light"),
              (80, "extralight"), (96, "extralight"), (44, "extralight"), (52.8, "extralight"))


def prechauffer(h):
    """Prépare les polices au démarrage : la première carte ne doit pas coûter d'images au visage."""
    S = h / 720.0
    for px, graisse in PRECHAUFFE:
        police(px * S, graisse)


def _image(url):
    """Pochette d'album : téléchargée en arrière-plan, la carte se redessine quand elle arrive."""
    if url in IMAGES: return IMAGES[url] or None
    IMAGES[url] = None

    def charger():
        global VERSION
        try:
            # Le jeton ne part QUE vers /plan/ : les pochettes d'album viennent du CDN de Spotify, et lui
            # envoyer notre secret serait pire que le problème qu'on corrige.
            requete = url
            if "/plan/" in url:
                requete = urllib.request.Request(url, headers={"Authorization": "Bearer " + _jeton()})
            with urllib.request.urlopen(requete, timeout=6) as r:
                IMAGES[url] = pygame.image.load(io.BytesIO(r.read(4_000_000)))
        except Exception:
            IMAGES[url] = False
        VERSION += 1
    threading.Thread(target=charger, daemon=True).start()
    return None


def _mettre_a_echelle(img, taille):
    """Redimensionne en douceur, quelle que soit la profondeur de l'image.

    pygame.transform.smoothscale refuse tout ce qui n'est pas en 24 ou 32 bits : un QR code arrive en palette
    1 bit, et le visage mourait dessus (21/09). Une image d'une autre source peut être dans n'importe quel format,
    donc la conversion se fait ici, une fois pour toutes.
    """
    if img.get_bitsize() < 24:
        img = img.convert_alpha()
    return pygame.transform.smoothscale(img, taille)


def _suivi(fnt, txt, couleur, ecart):
    """Un texte à lettres espacées. En petites capitales, c'est ce qui distingue une étiquette d'une phrase."""
    largeurs = [fnt.size(c)[0] for c in txt]
    s = pygame.Surface((max(1, sum(largeurs) + ecart * max(0, len(txt) - 1)), fnt.get_height()), pygame.SRCALPHA)
    x = 0
    for c, l in zip(txt, largeurs):
        s.blit(fnt.render(c, True, couleur), (x, 0)); x += l + ecart
    return s


FILET_L, FILET_E = 64, 2                  # longueur et épaisseur du filet de tête, en pixels @720p


def _etiquette(txt, S, util):
    """L'étiquette de tête : la place du filet, puis le mot en capitales espacées.

    Une carte n'est pas une liste de lignes : il lui faut une entrée en matière. Ce filet et ces capitales
    coûtent deux traits et posent toute la page (« c'est pauvre graphiquement », Greg, 21/09).

    Le filet n'est plus DESSINÉ ici : il rétrécit à chaque image pour dire le temps qui reste, donc il ne peut
    pas vivre dans une texture qu'on ne téléverse qu'une fois. On lui garde sa place et on rend son ordonnée,
    le shader du visage fait le reste. Un titre vide est un cas normal : le filet se dessine alors seul.
    """
    f = police(22 * S, "medium")
    filet, ecart = int(FILET_L * S), int(14 * S)
    # ~0,2 em : c'est l'espacement qui fait lire une suite de capitales comme une étiquette et pas comme un mot.
    mot = _suivi(f, _coupe(txt.upper(), f, util - filet - ecart), GRIS_CLAIR, max(1, int(22 * S * 0.2)))
    s = pygame.Surface((util, int(46 * S)), pygame.SRCALPHA)
    s.blit(mot, (filet + ecart, 0))
    return s, mot.get_height() // 2 - max(2, int(FILET_E * S)) // 2


CARDINAUX = {"nord": "N", "sud": "S", "est": "E", "ouest": "O", "nord-est": "N-E", "nord-ouest": "N-O",
             "sud-est": "S-E", "sud-ouest": "S-O"}


def _mesure(txt):
    """« à 915 m au nord-ouest » → « 915 m · N-O », ou None si ce n'est pas une distance.

    Écrite en toutes lettres, la distance prenait la moitié de la ligne et le nom du lieu finissait en
    « 3637 Rue… ». C'est pourtant le nom qui compte : la distance, Bulle la dit à voix haute.
    """
    m = re.fullmatch(r"(?:à\s+)?([\d ,.]+\s*(?:m|km))(?:\s+(?:au|à l'|vers le)\s+([\w-]+))?", txt.strip(), re.I)
    if not m:
        return None
    distance = re.sub(r"\s+", " ", m.group(1)).strip()
    cote = CARDINAUX.get((m.group(2) or "").lower())
    return f"{distance} · {cote}" if cote else distance


def _pastille(txt, S):
    """La distance, cerclée : le seul chiffre de la carte mérite d'être posé, pas aligné avec le reste."""
    f = police(24 * S, "light")
    larg, haut = f.size(txt)[0] + int(28 * S), int(40 * S)
    s = pygame.Surface((larg, haut), pygame.SRCALPHA)
    pygame.draw.rect(s, (*GRIS_CLAIR, 90), s.get_rect(), width=max(1, int(2 * S)), border_radius=haut // 2)
    s.blit(f.render(txt, True, GRIS_CLAIR), (int(14 * S), (haut - f.get_height()) // 2))
    return s


def _moins(txt):
    """Le signe moins typographique (U+2212) dans une valeur affichée.

    Le tiret du clavier est plus court et plus bas : à 96 px, « -4° » penche vers la gauche et ne s'aligne pas
    avec « 12° » de la colonne d'à côté. Uniquement ici : cerveau/cartes.py continue d'envoyer « -4° », que
    les tests attendent et que la voix ne lit de toute façon pas.
    """
    return str(txt).replace("-", "−")


def _tabulaire(fnt, txt, couleur):
    """Un nombre à avance fixe, chaque chiffre centré dans la largeur du plus large.

    Geist dessine ses chiffres PROPORTIONNELS par défaut (mesuré à 96 px : le « 1 » fait 35 px, le « 0 » en fait
    63) et SDL_ttf n'expose aucune fonctionnalité OpenType — « tnum » est hors d'atteinte. Sans ça, les trois
    colonnes d'une prévision ne tombent pas au même endroit et la carte paraît de travers.
    """
    pas = max(fnt.size(c)[0] for c in "0123456789")
    largeurs = [pas if c.isdigit() else fnt.size(c)[0] for c in txt]
    s = pygame.Surface((max(1, sum(largeurs)), fnt.get_height()), pygame.SRCALPHA)
    x = 0
    for c, l in zip(txt, largeurs):
        r = fnt.render(c, True, couleur)
        s.blit(r, (x + (l - r.get_width()) // 2, 0)); x += l
    return s


def _valeur(txt, px, couleur=BLANC):
    """La grosse valeur d'une carte `cles` : ExtraLight, chiffres tabulaires, « ° » réduit et remonté.

    À 96 px, le degré pleine taille pèse autant qu'un chiffre : l'œil lit « 4 ° » en deux temps au lieu d'une
    température. Réduit de ~45 % et posé en haut de la ligne, il redevient ce qu'il est — une unité.
    """
    txt = _moins(txt)
    corps, degre = (txt[:-1], txt[-1]) if txt.endswith("°") else (txt, "")
    f = police(px, "extralight")
    bloc = _tabulaire(f, corps, couleur)
    if not degre:
        return bloc
    d = police(px * 0.55, "extralight").render(degre, True, couleur)
    s = pygame.Surface((bloc.get_width() + d.get_width(), f.get_height()), pygame.SRCALPHA)
    s.blit(bloc, (0, 0)); s.blit(d, (bloc.get_width(), 0))   # y = 0 : le « ° » monte avec la ligne du petit corps
    return s


def _point(couleur, rayon):
    """Un disque net. pygame.draw.circle ne lisse pas ses bords : à 10 px de diamètre, l'octogone se voit —
    et c'est le seul corail qui reste dans la texture d'une carte, donc celui qu'on regarde."""
    g = pygame.Surface((rayon * 8, rayon * 8), pygame.SRCALPHA)
    pygame.draw.circle(g, couleur, (rayon * 4, rayon * 4), rayon * 4)
    return pygame.transform.smoothscale(g, (rayon * 2, rayon * 2))


def _espace(hauteur):
    return pygame.Surface((1, max(1, int(hauteur))), pygame.SRCALPHA)


def _coupe(txt, fnt, largeur):
    """Une seule ligne : on coupe au mot et on finit par « … » (jamais de deuxième ligne surprise)."""
    if fnt.size(txt)[0] <= largeur: return txt
    mots, sortie = txt.split(), ""
    for m in mots:
        essai = (sortie + " " + m).strip()
        if fnt.size(essai + "…")[0] > largeur: break
        sortie = essai
    return (sortie or txt[:1]) + "…"


def _lignes(txt, fnt, largeur, maxi=3):
    mots, lignes, cur = str(txt).split(), [], ""
    for m in mots:
        essai = (cur + " " + m).strip()
        if cur and fnt.size(essai)[0] > largeur:
            lignes.append(cur); cur = m
            if len(lignes) == maxi: break
        else:
            cur = essai
    if cur and len(lignes) < maxi: lignes.append(cur)
    return lignes or [""]


def _corps(n, S):
    """Densité : moins il y a de lignes, plus c'est gros. La v2 remplacera n par la distance mesurée."""
    return (58 if n <= 3 else 42 if n <= 6 else 32) * S


def _densite(items, largeur, hauteur, S):
    """La plus grande taille qui fasse tenir les lignes sans les couper (58 → 42 → 32 px).

    Un titre de tâche du CRM est long : à 42 px il finit en « Envoi 8 · ANA (Jean-Luc… », ce qui ne sert à rien.
    Mieux vaut descendre d'un cran et lire la ligne entière. La v2 bornera ce choix par la distance mesurée.
    """
    choix = None
    for taille in (58, 42, 32):
        px = taille * S
        f = police(px)
        # La clé est en Light, plus étroite que le corps : la mesurer en Regular réservait une gouttière trop
        # large et coupait des titres qui tenaient (« Envoi 8 · ANA (Jean-Luc… »).
        lcle = max([police(px, "light").size(str(i.get("cle", "")))[0] for i in items] + [0])
        dispo = largeur - lcle - (30 * S if lcle else 0)
        entiers = sum(1 for i in items if f.size(str(i.get("texte", "")))[0] <= dispo)
        choix = (px, f, lcle)
        if len(items) <= min(9, int(hauteur // (px * 1.42))) and entiers >= len(items) * 0.67:
            break
    return choix


def _bloc(surfaces, surf, x, haut_total):
    """Empile les lignes, centrées verticalement, et rend l'ordonnée de DÉPART (celle du filet de tête)."""
    y = depart = (surf.get_height() - haut_total) // 2
    for s, dx in surfaces:
        surf.blit(s, (x + dx, y)); y += s.get_height()
    return depart


def rendre(data, w, h):
    """Dessine la carte dans une surface w×h à fond transparent (le visage occupe l'autre côté de l'écran).

    Rend `(surface, filet)`, le filet étant le rectangle `(x, y, largeur, hauteur)` du trait de tête, en pixels
    de la carte : c'est le visage qui le trace, parce qu'il rétrécit à chaque image (compte à rebours).
    """
    S = h / 720.0
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    marge = int(72 * S)
    util = w - 2 * marge
    gabarit = str(data.get("gabarit", "liste"))
    lignes = []                                   # (surface, décalage x) empilées et centrées verticalement

    # Toute carte porte le filet de tête : c'est lui qui dit le temps qui reste. Sans titre, il se dessine seul,
    # à la place qu'aurait prise l'étiquette — une carte ne commence donc jamais à ras du texte.
    etiquette, y_filet = _etiquette(str(data.get("titre") or "").strip(), S, util)
    lignes.append((etiquette, 0))

    items = data.get("items") or []
    if gabarit == "liste":
        # Gouttière du point « prochain » : réservée sur TOUTES les lignes, sinon la seule ligne marquée serait
        # aussi la seule décalée. Rien à réserver quand rien n'est marqué — une liste de courses garde ses 28 px.
        gout = int(28 * S) if any(it.get("prochain") for it in items) else 0
        px, f, lcle = _densite(items, util - gout, h - 2 * marge - 46 * S, S)
        fc = police(px, "light")
        ecart = int(30 * S) if lcle else 0
        # 9 lignes est le maximum lisible d'un coup d'œil ; au-delà, la voix (ou un QR en v2) fait mieux
        maxi = max(1, min(9, int((h - 2 * marge - 46 * S) // (px * 1.42))))
        for it in items[:maxi]:
            ligne = pygame.Surface((util, int(px * 1.42)), pygame.SRCALPHA)
            if it.get("prochain"):
                r = max(3, int(5 * S))
                ligne.blit(_point(ACCENT, r), (int(9 * S) - r, int(fc.get_height() * 0.55) - r))
            if lcle:
                ligne.blit(fc.render(str(it.get("cle", "")), True, SOURD if it.get("passe") else GRIS_CLAIR), (gout, 0))
            ligne.blit(f.render(_coupe(str(it.get("texte", "")), f, util - gout - lcle - ecart), True,
                                SOURD if it.get("passe") else BLANC), (gout + lcle + ecart, 0))
            lignes.append((ligne, 0))
        if len(items) > maxi:
            f2 = police(30 * S, "light")
            lignes.append((_rendu(f2, f"+ {len(items) - maxi} autres", GRIS_CLAIR, 40 * S), 0))

    elif gabarit == "cles":
        if len(items) <= 3 and items:             # peu de valeurs : en colonnes, gros chiffres
            px = int((96 if len(items) < 3 else 80) * S)
            col = util // max(1, len(items))
            fk, fv, fn = police(30 * S, "light"), police(px, "extralight"), police(30 * S, "light")
            yv, notes = int(38 * S), any(it.get("note") for it in items)
            yn = yv + fv.get_height() + int(10 * S)
            bloc = pygame.Surface((util, yn + fn.get_height() if notes else yv + fv.get_height()), pygame.SRCALPHA)
            for i, it in enumerate(items):
                x = i * col
                bloc.blit(fk.render(_coupe(str(it.get("cle", "")), fk, col - 10), True, GRIS_CLAIR), (x, 0))
                bloc.blit(_valeur(str(it.get("valeur", "")), px), (x, yv))
                if it.get("note"):
                    bloc.blit(fn.render(_coupe(str(it["note"]), fn, col - 10), True, BLANC_ATTENUE), (x, yn))
            lignes.append((bloc, 0))
        else:                                     # beaucoup de valeurs : une par ligne, valeur alignée à droite
            px = _corps(len(items), S) * 0.8
            f, fk = police(px), police(px, "light")
            for it in items[:8]:
                ligne = pygame.Surface((util, int(px * 1.5)), pygame.SRCALPHA)
                val = f.render(_moins(it.get("valeur", "")), True, BLANC)
                ligne.blit(fk.render(_coupe(str(it.get("cle", "")), fk, util - val.get_width() - 30 * S), True, GRIS_CLAIR), (0, 0))
                ligne.blit(val, (util - val.get_width(), 0))
                lignes.append((ligne, 0))

    elif gabarit == "lieu":
        # Le plan prend TOUTE la largeur, le texte dessous : à 320 px de large et sans nom de rue, il ne servait à
        # rien (retour de Greg, 21/09). Un QR code passe par le même gabarit.
        img = _image(data["image"]) if data.get("image") else None
        place = int(min(util * 0.68, h - 2 * marge - 160 * S))
        if img:
            # à l'échelle de l'image, jamais étirée : un plan déformé se lit faux (les rues ne sont plus à angle droit)
            iw, ih = img.get_size()
            # Un plan large occupe toute la place ; une image carrée (un QR code) mangeait l'écran alors qu'elle
            # n'a pas besoin d'être grande pour se scanner à 3,5 m (retour de Greg, 21/09).
            if iw < ih * 1.3: place = int(place * 0.72)
            larg = min(util, int(place * iw / ih))
            vue = _mettre_a_echelle(img, (larg, int(larg * ih / iw)))
        else:
            vue = pygame.Surface((util, place), pygame.SRCALPHA)
            vue.fill((26, 26, 26))
        lignes.append((vue, 0))     # aligné sur le texte : un QR centré au-dessus d'un texte à gauche flotte
        lignes.append((_espace(22 * S), 0))
        f1, f2 = police(44 * S), police(26 * S, "light")
        # Le nom et la distance sur la MÊME ligne, la distance cerclée à droite : empilées, elles se valaient
        # toutes les deux et la carte n'avait pas de point d'entrée.
        accent = str(data.get("accent") or "").strip()
        pastille = _pastille(_mesure(accent) or accent, S) if accent else None
        # une pastille qui mange la ligne n'en est plus une : la phrase repasse alors sous l'adresse
        sous_le_texte = pastille is not None and pastille.get_width() > util * 0.45
        if sous_le_texte: pastille = None
        place = util - (pastille.get_width() + int(24 * S) if pastille else 0)
        nom = _coupe(str(data.get("texte", "")), f1, place)
        ligne = pygame.Surface((util, int(44 * S * 1.3)), pygame.SRCALPHA)
        ligne.blit(f1.render(nom, True, BLANC), (0, 0))
        if pastille:
            ligne.blit(pastille, (util - pastille.get_width(), (f1.get_height() - pastille.get_height()) // 2))
        lignes.append((ligne, 0))
        for l in _lignes(str(data.get("note") or ""), f2, util, 1):
            lignes.append((_rendu(f2, l, GRIS_CLAIR, 26 * S * 1.45), 0))
        if sous_le_texte:
            lignes.append((_rendu(f2, _coupe(accent, f2, util), GRIS_CLAIR, 26 * S * 1.5), 0))

    elif gabarit == "media":
        cote = int(200 * S)
        bloc = pygame.Surface((util, cote), pygame.SRCALPHA)
        img = _image(data["image"]) if data.get("image") else None
        pochette = pygame.Surface((cote, cote), pygame.SRCALPHA)
        pochette.fill((42, 42, 42))
        if img: pochette = _mettre_a_echelle(img, (cote, cote))
        bloc.blit(pochette, (0, 0))
        x = cote + int(44 * S)
        f1, f2, f3 = police(56 * S), police(32 * S, "light"), police(32 * S, "light")
        y = (cote - int(56 * S * 1.3 + 32 * S * 1.4)) // 2
        bloc.blit(f1.render(_coupe(str(data.get("texte", "")), f1, util - x), True, BLANC), (x, y))
        bloc.blit(f2.render(_coupe(str(data.get("note") or ""), f2, util - x), True, GRIS_CLAIR), (x, y + int(56 * S * 1.35)))
        if data.get("accent"):
            bloc.blit(f3.render(_coupe(str(data["accent"]), f3, util - x), True, GRIS_CLAIR),
                      (x, y + int(56 * S * 1.35 + 32 * S * 1.5)))
        lignes.append((bloc, 0))

    else:                                         # texte : nom propre, épellation, phrase courte
        txt = str(data.get("texte", ""))
        if data.get("epeler"):                    # lettre par lettre, couleurs alternées : l'œil découpe le mot
            f = police(76 * S)
            ecart = int(22 * S)
            largeurs = [f.size(c)[0] for c in txt]
            bloc = pygame.Surface((max(1, sum(largeurs) + ecart * max(0, len(txt) - 1)), int(76 * S * 1.35)), pygame.SRCALPHA)
            x = 0
            for i, c in enumerate(txt):
                # Alternance blanc / gris clair : l'œil découpe le mot aussi bien qu'avec le corail, qui n'a
                # plus rien à faire ici — il ne dit que ce que Bulle fait (21/09).
                bloc.blit(f.render(c, True, GRIS_CLAIR if i % 2 else BLANC), (x, 0)); x += largeurs[i] + ecart
            lignes.append((bloc, 0))
        else:
            f = police(56 * S)
            for l in _lignes(txt, f, util, 4):
                lignes.append((_rendu(f, l, BLANC, 56 * S * 1.32), 0))
        if data.get("note"):
            f2 = police(32 * S, "light")
            lignes.append((_rendu(f2, _coupe(str(data["note"]), f2, util), GRIS_CLAIR, 46 * S), 0))

    haut = sum(s.get_height() for s, _ in lignes)
    depart = _bloc(lignes, surf, marge, haut)
    return surf, (marge, depart + y_filet, int(FILET_L * S), max(2, int(FILET_E * S)))


def _rendu(fnt, txt, couleur, hauteur):
    """Un texte dans une surface de hauteur imposée (interligne maîtrisé)."""
    s = pygame.Surface((max(1, fnt.size(txt)[0]), int(hauteur)), pygame.SRCALPHA)
    s.blit(fnt.render(txt, True, couleur), (0, 0))
    return s


# Cartes de démonstration : touche C du visage (contrôle de lisibilité sur la TV, sans le cerveau).
EXEMPLES = [
    {"gabarit": "liste", "titre": "Courses", "items": [
        {"cle": "1", "texte": "Lait d'avoine"}, {"cle": "2", "texte": "Pois chiches"},
        {"cle": "3", "texte": "Citrons verts"}, {"cle": "4", "texte": "Pain de seigle"}, {"cle": "5", "texte": "Café"}]},
    {"gabarit": "liste", "titre": "Aujourd'hui", "items": [
        {"cle": "9 h 30", "texte": "Point d'équipe", "passe": True}, {"cle": "14 h 00", "texte": "Dentiste"},
        {"cle": "18 h 45", "texte": "Hockey"}]},
    {"gabarit": "cles", "titre": "Montréal", "items": [
        {"cle": "demain", "valeur": "-4°", "note": "neige"}, {"cle": "lundi", "valeur": "-9°", "note": "dégagé"},
        {"cle": "mardi", "valeur": "-2°", "note": "pluie"}]},
    {"gabarit": "media", "texte": "Digital Love", "note": "Daft Punk · Discovery", "accent": "CXN100", "duree": 0},
    {"gabarit": "texte", "titre": "Ça s'écrit", "texte": "Kyutai", "epeler": True, "note": "kyutai.org"},
    {"gabarit": "texte", "titre": "Avant de faire", "texte": "Éteindre les 14 lumières de la maison",
     "note": "dis « oui » pour confirmer", "duree": 0},
    {"gabarit": "lieu", "titre": "theatre", "texte": "Place des Arts", "note": "175 rue Sainte-Catherine Ouest, Montréal",
     "accent": "à 3,2 km au nord-est"},
]
