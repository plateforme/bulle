"""Plans : une vignette de carte, en noir et blanc avec un repère corail, pour les cartes « lieu ».

À 3,5 m, un plan détaillé est illisible — ce qui se lit, c'est le nom, l'adresse et la distance. Le plan n'est
donc qu'un repère visuel : tuiles OpenStreetMap passées en niveaux de gris inversés (fond sombre comme le
visage), un point corail au centre, rien d'autre.

Les tuiles sont mises en cache sur disque : la politique d'usage d'OSM demande un User-Agent identifiable et
interdit le téléchargement en masse. Une demande de lieu = au plus 6 tuiles, et jamais deux fois les mêmes.
"""
import hashlib, json, logging, math, os, re, time, urllib.parse, urllib.request
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter, ImageOps

TUILES = os.environ.get("BULLE_TUILES", os.path.expanduser("~/kinectface/etat/tuiles"))
PLANS = os.environ.get("BULLE_PLANS", os.path.expanduser("~/kinectface/etat/plans"))
RUES = os.environ.get("BULLE_RUES", os.path.expanduser("~/kinectface/etat/rues"))
UA = "BulleAI/1.0 (assistant vocal domestique ; contact : contact@example.org)"
SOURCE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
CORAIL = (255, 106, 77)
BLANC = (242, 238, 230)


def _xy(lat, lon, z):
    """Coordonnées de tuile (fractionnaires) en projection Web Mercator."""
    n = 2 ** z
    r = math.radians(lat)
    return (lon + 180) / 360 * n, (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n


def _tuile(z, x, y):
    chemin = os.path.join(TUILES, str(z), str(x), f"{y}.png")
    if os.path.exists(chemin):
        return Image.open(chemin).convert("RGB")
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    req = urllib.request.Request(SOURCE.format(z=z, x=x, y=y), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = r.read()
    with open(chemin, "wb") as f:
        f.write(data)
    return Image.open(BytesIO(data)).convert("RGB")


def _sombre(img):
    """Niveaux de gris inversés : le fond blanc d'OSM devient noir, les rues et les noms ressortent en clair."""
    g = ImageOps.invert(ImageOps.grayscale(img))
    return Image.merge("RGB", [g.point(lambda v: int(v * k)) for k in (0.92, 0.90, 0.86)])


# Le plan est DESSINÉ, pas photographié : deux épaisseurs de trait, aucun aplat, aucune étiquette. Une tuile OSM,
# même passée en gris, garde tout son encombrement d'origine (bâtiments, parcs, icônes) — illisible à 3,5 m sur
# une vignette de 320 px. On ne garde que la géométrie des rues, et la hiérarchie se lit à l'épaisseur.
# L'écart entre les deux familles doit être FRANC : quelques axes lumineux et épais, un réseau fin qui s'efface.
# À égalité d'épaisseur et de gris, le quadrillage résidentiel mange tout et on ne lit plus la structure.
TRAITS = {"motorway": (5, (242, 238, 230)), "trunk": (5, (242, 238, 230)), "primary": (4, (232, 228, 220)),
          "secondary": (3, (186, 182, 176)), "tertiary": (2, (150, 147, 142)),
          "residential": (1, (98, 95, 91)), "unclassified": (1, (98, 95, 91))}
FOND = (13, 13, 13)
FONDU = 0.22                    # part du petit côté consacrée au dégradé des bords (voir _etendue)


def _latlon(x, y, z):
    """Inverse de _xy : coin de tuile → coordonnées."""
    n = 2 ** z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))), x / n * 360 - 180


def _classes(zoom):
    """Un plan généralise : à l'échelle d'une ville on ne dessine pas les rues résidentielles.

    C'est autant une question de lisibilité que de charge — demander tout le réseau sur un grand cadre faisait
    répondre 504 à Overpass (21/09).
    """
    if zoom >= 15: return list(TRAITS)
    if zoom >= 13: return ["motorway", "trunk", "primary", "secondary", "tertiary"]
    if zoom >= 11: return ["motorway", "trunk", "primary"]
    return ["motorway", "trunk"]


def _rues(lat, lon, largeur, hauteur, zoom):
    """La géométrie des rues du cadre, en une seule requête Overpass (« out geom » donne les points en ligne)."""
    x, y = _xy(lat, lon, zoom)
    dx, dy = (largeur / 2) / 256 * 1.15, (hauteur / 2) / 256 * 1.15
    nord, ouest = _latlon(x - dx, y - dy, zoom)
    sud, est = _latlon(x + dx, y + dy, zoom)
    q = ('[out:json][timeout:25];way(%.5f,%.5f,%.5f,%.5f)["highway"~"^(%s)$"];out geom;'
         % (sud, ouest, nord, est, "|".join(_classes(zoom))))
    # Overpass est un service public et bénévole : il répond 429 dès qu'on insiste. Le même cadre redemandé
    # (Greg qui relance une adresse, un plan re-rendu) ne doit plus rien lui coûter — un mois de cache suffit,
    # le réseau de rues ne bouge pas à cette échelle.
    cache = os.path.join(RUES, hashlib.sha1(q.encode()).hexdigest()[:16] + ".json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 30 * 86400:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    for essai in range(2):
        req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(), headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                elements = json.load(r).get("elements") or []
            os.makedirs(RUES, exist_ok=True)
            with open(cache + ".tmp", "w", encoding="utf-8") as f:
                json.dump(elements, f)
            os.replace(cache + ".tmp", cache)
            return elements
        except Exception:
            if essai: raise
            time.sleep(2)            # Overpass est public : on lui laisse le temps de souffler, une fois


POLICES = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf")
NOM_RUE = (190, 186, 178)


def _police(px):
    from PIL import ImageFont
    for c in POLICES:
        try: return ImageFont.truetype(c, px)
        except OSError: pass
    return ImageFont.load_default()


def _texte_tourne(img, xy, texte, angle, police, couleur=NOM_RUE):
    """Écrit le long d'une rue : le texte suit son inclinaison, avec un liseré du fond pour rester lisible dessus."""
    from PIL import Image, ImageDraw
    d0 = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    l, h = int(d0.textlength(texte, font=police)) + 12, police.size + 10
    vignette = Image.new("RGBA", (l, h), (0, 0, 0, 0))
    ImageDraw.Draw(vignette).text((6, 4), texte, font=police, fill=couleur, stroke_width=3, stroke_fill=FOND)
    if abs(angle) > 1:
        vignette = vignette.rotate(angle, expand=True, resample=Image.BICUBIC)
    img.paste(vignette, (int(xy[0] - vignette.width / 2), int(xy[1] - vignette.height / 2)), vignette)
    return (xy[0] - vignette.width / 2, xy[1] - vignette.height / 2,
            xy[0] + vignette.width / 2, xy[1] + vignette.height / 2)


def _chevauche(boite, prises):
    return any(not (boite[2] < p[0] or boite[0] > p[2] or boite[3] < p[1] or boite[1] > p[3]) for p in prises)


TYPES_VOIE = ("Rue", "Avenue", "Boulevard", "Chemin", "Place", "Allée", "Impasse", "Ruelle", "Voie", "Côte", "Montée")
CARDINAUX = {"Est": "E.", "Ouest": "O.", "Nord": "N.", "Sud": "S.",
             "Nord-Est": "N.-E.", "Nord-Ouest": "N.-O.", "Sud-Est": "S.-E.", "Sud-Ouest": "S.-O."}


def _court(nom):
    """Le nom tel qu'un plan l'écrit : « Rue Sainte-Catherine Est » → « Ste-Catherine E. ».

    Le type de voie ne distingue rien (tout est « Rue ») et mange la place ; écrit en entier, le nom de la rue
    de destination ne rentrait pas entre le repère et le bord du cadre, donc ne s'affichait pas du tout.
    """
    mots = nom.split()
    if len(mots) > 1 and mots[0] in TYPES_VOIE:
        mots = mots[1:]
    if len(mots) > 1 and mots[-1] in CARDINAUX:
        mots[-1] = CARDINAUX[mots[-1]]
    court = " ".join(mots)
    for long, bref in (("Saint-", "St-"), ("Sainte-", "Ste-")):
        court = court.replace(long, bref)
    return court or nom


def _recoller(morceaux, tolerance=3.0):
    """Recolle les tronçons d'une même rue bout à bout → les tracés continus, du plus long au plus court.

    OSM redécoupe une rue à chaque changement d'attribut : sur un plan de quartier, le boulevard Pie-IX arrive en
    une douzaine de « ways » de 100 px alors qu'il traverse tout le cadre. Sans recollage, aucun tronçon n'est
    assez long pour porter son propre nom et le plan finit muet (vu le 21/09).
    """
    restants = [list(m) for m in morceaux if len(m) > 1]
    chaines = []
    while restants:
        chaine = restants.pop()
        colle = True
        while colle:
            colle = False
            for autre in list(restants):
                for sens in ("fin", "debut"):
                    bout = chaine[-1] if sens == "fin" else chaine[0]
                    for i in (0, -1):
                        if math.dist(bout, autre[i]) > tolerance: continue
                        # on retourne le tronçon pour qu'il commence — ou finisse — par le point commun
                        if sens == "fin":
                            chaine = chaine + (autre if i == 0 else autre[::-1])[1:]
                        else:
                            chaine = (autre[::-1] if i == 0 else autre)[:-1] + chaine
                        restants.remove(autre); colle = True
                        break
                    if colle: break
                if colle: break
        chaines.append(chaine)
    return sorted(chaines, key=len, reverse=True)


def _portion_visible(pts, largeur, hauteur, marge=20):
    """La plus longue suite de points consécutifs dans le cadre → (longueur, points).

    On ne filtre pas point par point : une rue qui sort du cadre et y revient produirait un faux segment droit
    qui traverse le plan, et l'étiquette se poserait dans le vide.
    """
    dedans = lambda q: -marge < q[0] < largeur + marge and -marge < q[1] < hauteur + marge
    meilleur, suite = (0.0, []), []
    for q in list(pts) + [None]:
        if q is not None and dedans(q):
            suite.append(q); continue
        if len(suite) > 1:
            lg = sum(math.dist(suite[i], suite[i + 1]) for i in range(len(suite) - 1))
            if lg > meilleur[0]: meilleur = (lg, suite)
        suite = []
    return meilleur


def _etendue(largeur, hauteur):
    """Longueur du dégradé des bords, en pixels.

    Proportionnelle au plan : à 34 px fixes la coupure restait franche et le plan avait encore l'air d'une
    vignette (« le dégradé pourrait être plus long ? », Greg, 21/09). Comparé à 34, 90 et 150 sur le même plan :
    au-delà, les rues du bord s'effacent avec le fond et on perd des repères.
    """
    return max(24, int(min(largeur, hauteur) * FONDU))


def _fondre(img, largeur, hauteur):
    """Le plan n'a pas de bord : ses quatre côtés s'effacent en transparence.

    Fondu vers une couleur de fond, le plan restait une vignette rectangulaire posée sur la carte — Greg le voit
    comme un cadre (21/09). En transparence, il n'y a plus de rectangle du tout : le dessin naît du noir de Bulle.
    """
    etendue = _etendue(largeur, hauteur)
    plein = etendue * 0.45          # le plein reste large : seule la descente s'allonge
    masque = Image.new("L", (largeur, hauteur), 0)
    ImageDraw.Draw(masque).rectangle((plein, plein, largeur - plein, hauteur - plein), fill=255)
    rgba = img.convert("RGBA")
    rgba.putalpha(masque.filter(ImageFilter.GaussianBlur(etendue * 0.6)))
    return rgba


def _noms(img, traces, largeur, hauteur, maxi=14, reserve=()):
    """Écrit le nom des rues sur leur tracé — un plan sans nom ne sert à rien (retour de Greg, 21/09).

    Une étiquette par rue, posée sur son plus long tracé continu visible ; celles qui tomberaient sur une autre
    ou sur le repère central sont sautées.
    """
    police = _police(max(13, int(hauteur / 22)))
    prises = [(largeur / 2 - 34, hauteur / 2 - 34, largeur / 2 + 34, hauteur / 2 + 34)]   # le repère d'abord
    prises += list(reserve)                          # …et ce qui est déjà dessiné (la barre d'échelle)
    par_nom = {}
    for nom, epaisseur, pts in traces:
        e, morceaux = par_nom.setdefault(nom, (epaisseur, []))
        morceaux.append(pts)
        if epaisseur > e: par_nom[nom] = (epaisseur, morceaux)
    candidats = []
    for nom, (epaisseur, morceaux) in par_nom.items():
        for chaine in _recoller(morceaux):
            longueur, visible = _portion_visible(chaine, largeur, hauteur)
            if longueur: candidats.append((nom, epaisseur, longueur, visible))
    garde = {}
    for nom, epaisseur, longueur, visible in candidats:      # le plus long tracé continu de chaque rue
        if longueur > garde.get(nom, (0,))[0]: garde[nom] = (longueur, epaisseur, visible)
    poses, ecrits = 0, set()
    # les axes d'abord (ce sont eux les repères), puis les tracés les plus longs
    for nom, (longueur, epaisseur, dedans) in sorted(garde.items(), key=lambda kv: (-kv[1][1], -kv[1][0])):
        if poses >= maxi: break
        nom = _court(nom)
        if nom in ecrits: continue        # « Rue X » et « Avenue X » s'abrègent pareil : une seule étiquette
        besoin = police.getlength(nom)
        if longueur < besoin * 1.05: continue
        # Plusieurs points d'ancrage le long du tracé : la rue de la destination passe sous le repère, et avec le
        # seul mi-parcours elle perdait son étiquette — c'est pourtant la plus utile du plan.
        for fraction in (0.5, 0.32, 0.68, 0.16, 0.84):
            cible, cumul, i = longueur * fraction, 0.0, 0
            while i < len(dedans) - 2 and cumul + math.dist(dedans[i], dedans[i + 1]) < cible:
                cumul += math.dist(dedans[i], dedans[i + 1]); i += 1
            a, b = dedans[i], dedans[i + 1]
            # le point à la bonne distance SUR le segment, et non son milieu : sur un tracé rectiligne (deux
            # points), les cinq essais retombaient au même endroit et la rue perdait son nom
            part = min(1.0, (cible - cumul) / (math.dist(a, b) or 1))
            milieu = (a[0] + (b[0] - a[0]) * part, a[1] + (b[1] - a[1]) * part)
            if not (8 < milieu[0] < largeur - 8 and 8 < milieu[1] < hauteur - 8): continue
            angle = math.degrees(math.atan2(-(b[1] - a[1]), b[0] - a[0]))
            if angle > 90: angle -= 180
            if angle < -90: angle += 180
            # l'encombrement d'un texte incliné n'est pas celui du texte à plat : sans cette rotation de la boîte,
            # un nom écrit à la verticale se posait en travers du repère central
            l, h = besoin * 1.1, police.size * 1.7
            c, si = abs(math.cos(math.radians(angle))), abs(math.sin(math.radians(angle)))
            l, h = l * c + h * si, l * si + h * c
            boite = (milieu[0] - l / 2, milieu[1] - h / 2, milieu[0] + l / 2, milieu[1] + h / 2)
            if boite[0] < 4 or boite[1] < 4 or boite[2] > largeur - 4 or boite[3] > hauteur - 4: continue
            if _chevauche(boite, prises): continue
            prises.append(_texte_tourne(img, milieu, nom, angle, police))
            poses += 1; ecrits.add(nom)
            break


def _echelle(img, lat, zoom, largeur, hauteur):
    """Barre d'échelle : sans elle, on ne sait pas si le plan montre un pâté de maisons ou un quartier."""
    from PIL import ImageDraw
    m_par_px = 156543.03392 * math.cos(math.radians(lat)) / 2 ** zoom   # tuiles de 256 px : la formule standard
    # la PLUS GRANDE graduation qui tienne dans le quart de l'image : en prenant la première qui dépasse, la barre
    # traversait le tiers du plan et annonçait « 1 km » là où 500 m suffisaient (vu le 21/09)
    metres, px = 50, 50 / m_par_px
    for candidat in (100, 200, 500, 1000, 2000, 5000):
        large = candidat / m_par_px
        if large > largeur * 0.26: break
        metres, px = candidat, large
    d = ImageDraw.Draw(img)
    marge = _etendue(largeur, hauteur) * 0.55
    x, y = marge, hauteur - marge              # hors du dégradé, sinon la barre s'efface avec le bord
    d.line([(x, y), (x + px, y)], fill=NOM_RUE, width=3)
    for bout in (x, x + px):
        d.line([(bout, y - 5), (bout, y + 5)], fill=NOM_RUE, width=3)
    texte = f"{metres} m" if metres < 1000 else f"{metres // 1000} km"
    police = _police(max(12, int(hauteur / 24)))
    d.text((x + px + 8, y - 9), texte, font=police, fill=NOM_RUE, stroke_width=3, stroke_fill=FOND)
    return (x - 4, y - police.size, x + px + 12 + d.textlength(texte, font=police), y + police.size / 2)


def _dessiner(lat, lon, largeur, hauteur, zoom):
    """→ image des rues, ou None si les données manquent (on se rabat alors sur les tuiles)."""
    rues = _rues(lat, lon, largeur, hauteur, zoom)
    if len(rues) < 4:
        return None
    cx, cy = _xy(lat, lon, zoom)
    img = Image.new("RGB", (largeur, hauteur), FOND)
    d = ImageDraw.Draw(img)
    traces = []
    # les petites rues d'abord, les axes par-dessus : la hiérarchie doit rester lisible aux croisements
    for epaisseur in (1, 2, 3, 4, 5):
        for w in rues:
            tags = w.get("tags") or {}
            trait = TRAITS.get(tags.get("highway"))
            if not trait or trait[0] != epaisseur: continue
            pts = []
            for p in w.get("geometry") or []:
                px, py = _xy(p["lat"], p["lon"], zoom)
                pts.append(((px - cx) * 256 + largeur / 2, (py - cy) * 256 + hauteur / 2))
            if len(pts) > 1:
                d.line(pts, fill=trait[1], width=trait[0], joint="curve")
                nom = (tags.get("name") or "").strip()
                if nom and len(nom) < 34:
                    traces.append((nom, epaisseur, pts))
    # l'échelle d'abord : les noms de rues doivent pouvoir l'éviter, pas se faire barrer par elle
    _noms(img, traces, largeur, hauteur, reserve=[_echelle(img, lat, zoom, largeur, hauteur)])
    return _fondre(img, largeur, hauteur)


def qr(texte, cote=460):
    """QR code d'un lien, dessiné aux couleurs de Bulle → (chemin, clé). Servi par la même route que les plans.

    Dessiné ici plutôt qu'enregistré par segno, pour deux raisons : son PNG sort en palette 1 bit, que le visage
    ne sait pas mettre à l'échelle (il en mourait, 21/09), et on veut le style de Bulle — une plaque beige aux
    coins arrondis, et sa bulle corail au centre.

    La plaque est claire et les modules sombres, dans ce sens-là et pas l'inverse : en négatif, le code est plus
    beau sur la TV mais les lecteurs ne le retrouvent plus (vérifié, OpenCV ne décode que l'image ré-inversée).
    L'emblème central est permis par la correction d'erreur « h » (30 % du code reste redondant).
    """
    import segno
    cle = hashlib.sha1(("qr3:" + texte).encode()).hexdigest()[:16]
    chemin = os.path.join(PLANS, cle + ".png")
    if os.path.exists(chemin):
        return chemin, cle
    matrice = [list(ligne) for ligne in segno.make(texte, error="h").matrix]
    n = len(matrice)
    bord = 4                                             # zone de silence : sans elle, rien ne se scanne
    m = max(4, cote // (n + 2 * bord))
    taille = (n + 2 * bord) * m
    img = Image.new("RGBA", (taille, taille), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, taille - 1, taille - 1), radius=m * 3, fill=BLANC + (255,))
    for y in range(n):
        for x in range(n):
            if matrice[y][x]:
                gx, gy = (bord + x) * m, (bord + y) * m
                d.rectangle((gx, gy, gx + m - 1, gy + m - 1), fill=FOND + (255,))
    r = taille * 0.075
    c = taille / 2
    d.ellipse((c - r - m, c - r - m, c + r + m, c + r + m), fill=BLANC + (255,))     # respiration autour
    d.ellipse((c - r, c - r, c + r, c + r), fill=CORAIL + (255,))
    os.makedirs(PLANS, exist_ok=True)
    img.save(chemin + ".tmp", "PNG")
    os.replace(chemin + ".tmp", chemin)
    return chemin, cle


def cle_de(lat, lon, largeur, hauteur, zoom):
    """Clé déterministe : le cerveau peut donner l'adresse du plan AVANT de l'avoir dessiné."""
    return hashlib.sha1(f"v3:{lat:.5f},{lon:.5f},{largeur}x{hauteur},{zoom}".encode()).hexdigest()[:16]


def vignette(lat, lon, largeur=560, hauteur=380, zoom=15):
    """Rend le plan et renvoie son chemin ; rien n'est retéléchargé si le fichier existe déjà."""
    cle = cle_de(lat, lon, largeur, hauteur, zoom)
    chemin = os.path.join(PLANS, cle + ".png")
    if os.path.exists(chemin):
        return chemin, cle
    try:
        img = _dessiner(lat, lon, largeur, hauteur, zoom)
    except Exception as e:
        logging.getLogger("cerveau").warning("plan vectoriel impossible (%s) : repli sur les tuiles", e)
        img = None
    if img is not None:
        return _repere(img, chemin, largeur, hauteur)
    # repli : Overpass muet ou zone sans données → les tuiles, qui valent mieux que pas de plan du tout
    fx, fy = _xy(lat, lon, zoom)
    # tuiles nécessaires autour du centre, avec une marge d'une tuile de chaque côté
    nx, ny = math.ceil(largeur / 256) + 2, math.ceil(hauteur / 256) + 2
    x0, y0 = int(fx) - nx // 2, int(fy) - ny // 2
    planche = Image.new("RGB", (nx * 256, ny * 256), (255, 255, 255))
    for i in range(nx):
        for j in range(ny):
            try:
                planche.paste(_tuile(zoom, (x0 + i) % 2 ** zoom, y0 + j), (i * 256, j * 256))
            except Exception:
                pass                                   # une tuile manquante laisse un carré vide, pas une panne
    cx, cy = (fx - x0) * 256, (fy - y0) * 256          # position exacte du lieu dans la planche
    img = _sombre(planche).crop((int(cx - largeur / 2), int(cy - hauteur / 2),
                                 int(cx - largeur / 2) + largeur, int(cy - hauteur / 2) + hauteur))
    return _repere(_fondre(img, largeur, hauteur), chemin, largeur, hauteur)


def _repere(img, chemin, largeur, hauteur):
    """Le point corail, seule couleur de l'image, et l'attribution."""
    # le repère grandit avec le plan : à 13 px fixes, il se perdait sur un plan de 1000 px de large
    mx, my, r = largeur // 2, hauteur // 2, max(13, largeur // 45)
    halo = Image.new("RGBA", img.size, (0, 0, 0, 0))                                 # détoure le repère des rues
    ImageDraw.Draw(halo).ellipse((mx - r - 9, my - r - 9, mx + r + 9, my + r + 9), fill=FOND + (255,))
    img.alpha_composite(halo.filter(ImageFilter.GaussianBlur(3)))
    d = ImageDraw.Draw(img)
    d.ellipse((mx - r, my - r, mx + r, my + r), outline=CORAIL, width=5)
    d.ellipse((mx - r // 4, my - r // 4, mx + r // 4, my + r // 4), fill=CORAIL)
    t = "© OpenStreetMap"                                                            # attribution obligatoire
    # l'attribution est obligatoire : discrète, mais pas noyée dans le fondu du bord
    marge = _etendue(largeur, hauteur) * 0.55
    d.text((largeur - marge - d.textlength(t), hauteur - marge - 10), t, fill=(96, 94, 90))
    os.makedirs(PLANS, exist_ok=True)
    img.save(chemin + ".tmp", "PNG")
    os.replace(chemin + ".tmp", chemin)      # jamais de fichier à moitié écrit : le Pi peut le demander à tout moment
    return chemin, os.path.basename(chemin)[:-4]


def geocoder(recherche, autour=None, ville_defaut=None):
    """Nominatim : texte libre → (nom, adresse, lat, lon, catégorie).

    Deux passes. D'abord BORNÉE à la région (autour = domicile ou ville de référence) : sans la borne, « 3637
    University » était trouvé à Charlottetown, à 800 km, et Bulle l'annonçait sans sourciller (21/09). Puis le
    monde entier, parce que « la Tour Eiffel » doit quand même être trouvée.

    Ce qui revient de la seconde passe est filtré : un résultat à plus de 150 km n'est retenu que s'il est
    NOTOIRE (l'importance de Nominatim : 0,62 pour la Tour Eiffel, 0,0 pour une succursale de banque homonyme)
    ou si Greg a lui-même nommé la ville où il est.
    """
    tag = categorie(recherche, ville_defaut)
    if tag and autour:                       # « une pharmacie » n'est pas un nom : c'est une recherche par catégorie
        # et si Overpass ne répond pas, on ne se rabat PAS sur une recherche par nom : « un dépanneur » trouvait
        # alors « Le ti dépanneur » à Saint-Pierre-et-Miquelon. Mieux vaut dire qu'on n'a rien trouvé.
        return proche(tag, autour)
    q = _requete(recherche)
    if autour:
        pres = _chercher(q, autour, borne=True)
        # « 3637 University » : une adresse dite sans sa ville ne se trouve pas toujours du premier coup. On
        # ajoute la ville de référence plutôt que d'aller la chercher à l'autre bout du pays.
        if not pres and ville_defaut and "," not in q:
            pres = _chercher(f"{q}, {ville_defaut}", autour, borne=True)
        if pres:
            return pres
    loin = _chercher(q, None)
    if loin and autour and distance_direction(autour, (loin["lat"], loin["lon"]))[0] > 150:
        ville = q.split(",")[-1].strip() if "," in q else ""
        if float(loin.get("importance") or 0) < 0.45 and not (ville and _sans_accent(ville) in _sans_accent(loin["complet"])):
            logging.getLogger("cerveau").info("« %s » : seul résultat à %d km et inconnu (%s) — on ne dit rien",
                                              recherche, distance_direction(autour, (loin["lat"], loin["lon"]))[0],
                                              loin["nom"])
            return None
    return loin


ARTICLES = re.compile(r"^\s*(?:l[ae]s?|l'|un[e]?|des|du|au[x]?|cette?|ce|mon|ma|mes)\s+", re.I)
# « Place des Arts À Montréal » ne renvoie RIEN chez Nominatim, « Place des Arts, Montréal » renvoie le bon lieu.
# C'est la cause de « la localisation ne fonctionne plus » (21/09) : la phrase parlée met naturellement « à ».
PREPOSITIONS = re.compile(r"\s+(?:à|a|dans|sur)\s+(?=\S)", re.I)


def _sans_accent(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(t).lower()) if unicodedata.category(c) != "Mn")


def _requete(recherche):
    """Le texte parlé → une requête que Nominatim comprend."""
    q = ARTICLES.sub("", str(recherche)).strip() or str(recherche)
    return PREPOSITIONS.sub(", ", q).strip()


def _nominatim(params):
    """L'appel réseau, isolé : les tests le remplacent pour rejouer de vraies réponses sans toucher au service."""
    req = urllib.request.Request(NOMINATIM + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": UA, "Accept-Language": "fr"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def _chercher(recherche, autour, borne=False):
    p = {"q": recherche, "format": "jsonv2", "limit": "1", "accept-language": "fr", "addressdetails": "1"}
    if autour:
        lat, lon = autour
        p["viewbox"] = f"{lon - 1.2},{lat + 0.8},{lon + 1.2},{lat - 0.8}"
        if borne: p["bounded"] = "1"
    res = _nominatim(p)
    if not res:
        return None
    d = res[0]
    a = d.get("address") or {}
    voie = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
    ville = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or ""
    nom = d.get("name") or (d.get("display_name") or "").split(",")[0]
    # Nominatim nomme « 3637 » le point d'une adresse : affiché en grand sur la TV, ce chiffre seul ne dit rien
    # (vu le 21/09). On lui rend sa rue.
    if re.fullmatch(r"\d+[a-zA-Z]?", nom.strip()) and voie:
        nom = voie
    return {"nom": nom, "adresse": ", ".join(x for x in (voie, ville) if x) or (d.get("display_name") or "")[:60],
            "lat": float(d["lat"]), "lon": float(d["lon"]),
            "importance": float(d.get("importance") or 0), "complet": d.get("display_name") or "",
            "categorie": TYPES.get(str(d.get("type") or ""), TYPES.get(str(d.get("category") or ""), ""))}


# Types OSM courants → un mot français. Un type inconnu (« yes », « hairdresser »…) ne s'affiche pas :
# mieux vaut pas de titre qu'un mot anglais brut au-dessus du nom.
TYPES = {"restaurant": "restaurant", "fast_food": "restauration rapide", "cafe": "café", "bar": "bar", "pub": "pub",
         "bakery": "boulangerie", "supermarket": "épicerie", "convenience": "dépanneur", "pharmacy": "pharmacie",
         "hospital": "hôpital", "clinic": "clinique", "doctors": "clinique", "dentist": "dentiste",
         "school": "école", "university": "université", "college": "cégep", "library": "bibliothèque",
         "theatre": "théâtre", "cinema": "cinéma", "museum": "musée", "artwork": "œuvre", "attraction": "attrait",
         "park": "parc", "garden": "jardin", "viewpoint": "point de vue", "hotel": "hôtel", "bank": "banque",
         "fuel": "station-service", "parking": "stationnement", "police": "poste de police", "post_office": "bureau de poste",
         "station": "gare", "bus_stop": "arrêt d'autobus", "subway": "métro", "aerodrome": "aéroport",
         "city": "ville", "town": "ville", "village": "village", "suburb": "quartier", "neighbourhood": "quartier",
         "place_of_worship": "lieu de culte", "church": "église", "sports_centre": "centre sportif", "stadium": "stade"}

DIRECTIONS = ["nord", "nord-est", "est", "sud-est", "sud", "sud-ouest", "ouest", "nord-ouest"]


def distance_direction(depuis, vers):
    """(km, direction cardinale) entre deux points."""
    (la1, lo1), (la2, lo2) = depuis, vers
    r1, r2 = math.radians(la1), math.radians(la2)
    dr, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = math.sin(dr / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dl / 2) ** 2
    km = 6371 * 2 * math.asin(min(1, math.sqrt(a)))
    cap = math.degrees(math.atan2(math.sin(dl) * math.cos(r2),
                                  math.cos(r1) * math.sin(r2) - math.sin(r1) * math.cos(r2) * math.cos(dl))) % 360
    return km, DIRECTIONS[int((cap + 22.5) % 360 // 45)]


def zoom_utile(km):
    """Plus c'est loin, plus on dézoome : à 40 km, un plan de rue ne veut plus rien dire."""
    for seuil, z in ((1, 16), (3, 15), (8, 14), (25, 12), (120, 10), (600, 8)):
        if km <= seuil: return z
    return 6


# Recherches par catégorie : Nominatim cherche des NOMS, pas des types. « Trouve-moi une pharmacie » lui renvoyait
# un commerce nommé « Une » à Valence. Overpass interroge les tags OSM autour d'un point, ce qui est la bonne question.
OVERPASS = "https://overpass-api.de/api/interpreter"
CATEGORIES = {"pharmacie": "amenity=pharmacy", "depanneur": "shop=convenience", "epicerie": "shop=supermarket",
              "supermarche": "shop=supermarket", "restaurant": "amenity=restaurant", "cafe": "amenity=cafe",
              "bar": "amenity=bar", "boulangerie": "shop=bakery", "essence": "amenity=fuel",
              "station service": "amenity=fuel", "hopital": "amenity=hospital", "clinique": "amenity=clinic",
              "dentiste": "amenity=dentist", "banque": "amenity=bank", "guichet": "amenity=atm",
              "bureau de poste": "amenity=post_office", "parc": "leisure=park", "bibliotheque": "amenity=library",
              "quincaillerie": "shop=doityourself", "saq": "shop=alcohol", "veterinaire": "amenity=veterinary",
              "hotel": "tourism=hotel", "musee": "tourism=museum", "cinema": "amenity=cinema"}


def _sans_accents(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(t).lower()) if unicodedata.category(c) != "Mn")


# Mots qui ne distinguent rien : ils entourent une demande générique sans la nommer.
MOTS_VIDES = {"le", "la", "les", "l", "un", "une", "des", "du", "de", "d", "au", "aux", "a", "en", "dans", "the",
              "pres", "proche", "proches", "plus", "ici", "autour", "moi", "nous", "chez", "vers", "cote",
              "ouvert", "ouverte", "ouverts", "ouvertes", "prochain", "prochaine", "prochains", "prochaines",
              "quartier", "coin", "environ", "pas", "loin", "trouve", "trouver", "cherche", "chercher", "montre",
              "montrer", "localise", "localiser", "situe", "situer", "est", "ou", "quel", "quelle", "bon", "bonne",
              "meilleur", "meilleure", "petit", "petite", "grand", "grande", "nouveau", "nouvelle"}
INDEFINI = re.compile(r"(?:^|\W)(un|une|des)\W", re.I)


def categorie(recherche, ville=None):
    """« trouve-moi une pharmacie » → amenity=pharmacy ; « le musée McCord » → None, c'est un NOM.

    Le mot de catégorie était cherché en simple sous-chaîne : « musée McCord » partait donc en recherche de
    musées alentour, qui renvoyait le bon musée mais SANS son adresse, et plus rien du tout dès qu'Overpass
    boudait — la même demande marchait ou pas selon la minute (vu le 21/09).

    Deux signaux tranchent : un déterminant indéfini annonce une catégorie (« UN restaurant italien »), et
    inversement un mot distinctif qui subsiste une fois retirés le mot de catégorie, la ville et les mots vides
    annonce un nom propre (« McCord », « du Parlement »).
    """
    n = _sans_accents(recherche)
    for mot, tag in sorted(CATEGORIES.items(), key=lambda kv: -len(kv[0])):
        if mot not in n: continue
        if INDEFINI.search(" " + n.split(mot)[0] + " "): return tag
        reste = n.replace(mot, " ")
        if ville: reste = reste.replace(_sans_accents(ville), " ")
        distinctifs = [m for m in re.findall(r"[a-z0-9'-]+", reste) if len(m) > 2 and m not in MOTS_VIDES]
        return None if distinctifs else tag
    return None


def proche(tag, autour, rayons=(3000, 12000)):
    """Le plus proche du point donné qui porte ce tag OSM."""
    cle, val = tag.split("=")
    lat, lon = autour
    for rayon in rayons:
        q = f'[out:json][timeout:25];nwr(around:{rayon},{lat},{lon})["{cle}"="{val}"];out center 40;'
        req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(),
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                elements = json.load(r).get("elements") or []
        except Exception:
            elements = []                    # Overpass limite le débit : on souffle, on tente le rayon suivant
            time.sleep(1.5)
        candidats = []
        for e in elements:
            t = e.get("tags") or {}
            c = e.get("center") or e
            if not t.get("name") or c.get("lat") is None: continue
            candidats.append((distance_direction((lat, lon), (c["lat"], c["lon"]))[0], t, c))
        if candidats:
            _, t, c = min(candidats, key=lambda x: x[0])
            voie = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
            return {"nom": t["name"], "lat": c["lat"], "lon": c["lon"],
                    "adresse": ", ".join(x for x in (voie, t.get("addr:city")) if x) or "",
                    "categorie": TYPES.get(val, "")}
    return None
