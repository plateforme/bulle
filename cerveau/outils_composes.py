"""Outils composés de Bulle : de petites fonctions qui enchaînent des outils de base (Home Assistant, Spotify…).

MODÈLE À SUIVRE pour ajouter un outil (l'agent local peut en ajouter ici, avec un test dans tests/banc.yaml) :

    @outil("nom_outil", "Ce que fait l'outil, quand l'utiliser (le LLM ne lit que ça).",
           {"param": {"type": "string", "description": "..."}}, requis=["param"], necessite=["spotify"])
    async def nom_outil(ctx, args):
        info = await ctx.json("get_playback", {})            # appel d'un outil existant (lecture)
        await ctx.ha("POST", "/api/services/…", {...})       # appel Home Assistant (simulé en mode test)
        return ctx.ok(fait="ce qui a été fait", non_fait=[])  # toujours dire ce qui a été fait / pas fait

Règles : jamais d'effet direct hors de ctx (ctx.outil / ctx.ha / ctx.spotify), sinon le mode simulation ne le voit pas ;
retourner ctx.ok(...) ou ctx.non_fait(...) ; les réglages chiffrés vont dans config/regles.yaml, pas dans le code.
"""
import json, os, random, re, time

import journal
import regles

OUTILS = {}


def outil(nom, description, parametres, requis=(), necessite=()):
    """necessite : noms d'outils de base requis, ou « spotify » / « ha »."""
    def deco(fn):
        OUTILS[nom] = dict(nom=nom, description=description, parametres=parametres, requis=list(requis),
                           necessite=list(necessite), fn=fn)
        return fn
    return deco


def _norm(t):
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", str(t).lower()) if unicodedata.category(ch) != "Mn").strip()


def correspondance(valeur, options):
    """« blanche » → Blanc, « neon » → Néon : sans accents ni casse, par préfixe."""
    v = _norm(valeur)
    if not v: return None
    for o in options:
        n = _norm(o)
        if v == n or v.startswith(n) or n.startswith(v): return o
    return None


# ---------------------------------------------------------------- lumières
def _compte(faits, participe):
    """« 1 lumières éteintes » se dit mal, et Bulle lit ce texte à voix haute."""
    return f"1 lumière {participe}" if len(faits) == 1 else f"{len(faits)} lumières {participe}s"


async def _lumieres(ctx, cle, outil, extra=None):
    """Agit sur les lumières UNE PAR UNE → (faites, ratées).

    La route eteindre_tout du serveur maison passe « entity_id: all » à Home Assistant : la syntaxe est valide,
    mais il suffit qu'un seul appareil ne réponde pas pour que HA renvoie 500 et que RIEN ne soit éteint
    (vu le 20/09 : « arrête toutes les lumières » a échoué deux fois de suite, les neuf lampes sont restées
    allumées). En énumérant, une ampoule injoignable ne coûte que sa propre ligne, et on peut le dire.
    """
    etat = await ctx.json("etat_maison", {})
    faits, rates = [], []
    for n in [x.strip() for x in etat.get(cle, []) if str(x).strip()]:
        try:
            r = await ctx.json(outil, dict({"nom": n}, **(extra or {})))
        except Exception:
            r = {}
        (faits if r.get("ok") else rates).append(r.get("appareil") or n)
    return faits, rates


@outil("allumer_tout", "Allume toutes les lumières de la maison, et règle leur luminosité quand Greg la donne (« toutes les "
       "lumières à 10 % », « baisse toutes les lumières ») — y compris celles qui sont déjà allumées. Uniquement pour TOUTES "
       "les lumières ; jamais pour une ambiance, une scène ou un autre appareil.",
       {"luminosite_pct": {"type": "integer", "description": "luminosité 1-100, seulement si Greg la demande"}},
       necessite=["allumer", "etat_maison"])
async def allumer_tout(ctx, args):
    lum = args.get("luminosite_pct")
    # Une lampe déjà allumée n'est pas dans « lumieres_eteintes » : sans ce deuxième passage, « toutes les lumières
    # à 10 % » ne faisait rien du tout quand elles étaient allumées, et Bulle annonçait quand même le changement
    # (vu le 21/09). Régler la luminosité, c'est agir sur TOUTES les lampes.
    cles = ["lumieres_eteintes"] + (["lumieres_allumees"] if lum is not None else [])
    faits, rates = [], []
    for cle in cles:
        f, r = await _lumieres(ctx, cle, "allumer", {"luminosite_pct": lum})
        faits += f; rates += r
    if not faits and not rates:
        return ctx.ok(fait="les lumières étaient déjà toutes allumées")
    fait = _compte(faits, "allumée") + (f", à {lum} %" if lum is not None else "")
    return ctx.ok(fait=fait, non_fait=[f"pas pu allumer : {', '.join(rates)}"] if rates else [])


@outil("eteindre_tout", "Éteint toutes les lumières de la maison. Uniquement si Greg demande explicitement d'éteindre toutes "
       "les lumières ; jamais pour une seule pièce ni pour un autre appareil.", {}, necessite=["eteindre", "etat_maison"])
async def eteindre_tout(ctx, args):
    faits, rates = await _lumieres(ctx, "lumieres_allumees", "eteindre")
    if not faits and not rates:
        return ctx.ok(fait="les lumières étaient déjà toutes éteintes")
    return ctx.ok(fait=_compte(faits, "éteinte"), non_fait=[f"pas pu éteindre : {', '.join(rates)}"] if rates else [])


# ---------------------------------------------------------------- panneau LED
async def _club_params(ctx):
    opts = {}
    for e in ("input_select.led_club_palette", "input_select.led_club_style"):
        try: opts[e] = (await ctx.ha("GET", f"/api/states/{e}"))["attributes"]["options"]
        except Exception: opts[e] = []
    ctx.memo["club_opts"] = opts
    return {"actif": {"type": "boolean", "description": "true = mode club allumé, false = coupé"},
            "palette": {"type": "string", "enum": opts["input_select.led_club_palette"], "description": "seulement si Greg nomme une palette"},
            "style": {"type": "string", "enum": opts["input_select.led_club_style"], "description": "seulement si Greg nomme un style"},
            "luminosite": {"type": "integer", "minimum": 10, "maximum": regles.get("cerveau", "club", {}).get("luminosite_max", 70),
                           "description": "seulement si Greg demande une luminosité"}}


@outil("mode_club", "Panneau LED du salon (les dalles) : active ou coupe le mode club (animations au rythme des basses du caisson), "
       "et règle sa palette, son style et sa luminosité. Seul outil pour le panneau LED.",
       _club_params, requis=["actif"], necessite=["ha"])
async def mode_club(ctx, args):
    club = regles.c("club", {})
    opts = ctx.memo.get("club_opts") or {}
    faits, non_faits = [], []
    for cle, ent in (("palette", "input_select.led_club_palette"), ("style", "input_select.led_club_style")):
        if args.get(cle):
            m = correspondance(args[cle], opts.get(ent, []))
            if m: args[cle] = m
            else:
                non_faits.append(f"{cle} « {args[cle]} » inconnue (choix : {', '.join(opts.get(ent, []))})"); args.pop(cle)
    # le LLM remplit souvent une luminosité de lui-même : on ne l'applique que si la phrase de Greg en parle
    if not isinstance(args.get("luminosite"), (int, float)) or not re.search(club.get("mots_luminosite", "lumin"), ctx.said, re.I):
        args.pop("luminosite", None)
    if args.get("palette"):
        await ctx.ha("POST", "/api/services/input_select/select_option", {"entity_id": "input_select.led_club_palette", "option": args["palette"]})
        faits.append("palette " + args["palette"])
    if args.get("style"):
        await ctx.ha("POST", "/api/services/input_select/select_option", {"entity_id": "input_select.led_club_style", "option": args["style"]})
        faits.append("style " + args["style"])
    if args.get("luminosite") is not None:
        v = max(10, min(club.get("luminosite_max", 70), int(args["luminosite"])))
        await ctx.ha("POST", "/api/services/input_number/set_value", {"entity_id": "input_number.led_club_luminosite", "value": v})
        faits.append(f"luminosité {v}")
    if args.get("actif") is not None:
        await ctx.ha("POST", f"/api/services/input_boolean/{'turn_on' if args['actif'] else 'turn_off'}", {"entity_id": "input_boolean.led_club"})
        faits.append("mode club " + ("activé" if args["actif"] else "coupé"))
    return ctx.ok(fait=", ".join(faits), non_fait=non_faits,
                  note="le panneau n'anime qu'avec de la musique captée par le capteur du caisson de basses")


# ---------------------------------------------------------------- mode nuit
@outil("mode_nuit", "Passe Bulle en veille de nuit (écran assombri, voix baissée, il faut dire « Bulle » pour lui parler) ou en sort. "
       "« mode nuit », « bonne nuit », « je vais me coucher » = actif ; « bonjour », « réveille-toi », « sors du mode nuit » = inactif. "
       "S'active aussi tout seul selon l'horaire de config/regles.yaml.",
       {"actif": {"type": "boolean", "description": "true = veille de nuit, false = réveil"}}, requis=["actif"])
async def mode_nuit(ctx, args):
    actif = bool(args.get("actif"))
    await ctx.envoyer({"type": "mode", "nuit": actif})
    return ctx.ok(fait="mode nuit activé" if actif else "mode nuit désactivé",
                  note="écran assombri, voix baissée et nom obligatoire" if actif else "écran et voix normaux")


# ---------------------------------------------------------------- musique (Spotify)
_cache = {"favoris_t": 0.0, "favoris": [], "vol": None, "vol_t": 0.0, "vol_dev": None, "domicile": None, "domicile_t": 0.0, "region": None}


async def favoris(ctx):
    if time.time() - _cache["favoris_t"] > 600 or not _cache["favoris"]:
        items = await ctx.json("get_saved_tracks", {}, mcp=True)
        _cache.update(favoris_t=time.time(), favoris=[i for i in items if isinstance(i, dict) and i.get("uri")])
    return _cache["favoris"]


def ressemblance(voulu, titre):
    """Titre demandé / titre (sans numéro de piste, accents, casse) — tolère une transcription imparfaite."""
    import difflib
    net = lambda t: re.sub(r"^\d+\s*[.)-]\s*", "", _norm(t))
    return difflib.SequenceMatcher(None, net(voulu), net(titre)).ratio()


@outil("jouer_favoris", "Joue les titres likés (favoris, « mes titres likés », « ma musique ») de Greg sur Spotify, dans un ordre aléatoire.",
       {}, necessite=["spotify"])
async def jouer_favoris(ctx, args):
    items = await favoris(ctx)
    n = regles.c("musique", {}).get("favoris_max", 100)
    uris = random.sample([i["uri"] for i in items], min(n, len(items)))
    await ctx.spotify("play", {"track_uris": uris})
    return ctx.ok(fait=f"{len(uris)} titres likés mélangés, sur {len(items)}")


@outil("jouer_morceau", "Joue un morceau précis : cherche d'abord dans les titres likés de Greg (tolère les erreurs de transcription), "
       "puis sur tout Spotify. À utiliser pour « joue <titre> », avec ou sans « dans mes titres likés ».",
       {"titre": {"type": "string"}, "artiste": {"type": "string", "description": "si Greg le précise"}}, requis=["titre"], necessite=["spotify"])
async def jouer_morceau(ctx, args):
    voulu, art = args.get("titre", ""), _norm(args.get("artiste") or "")
    notes = sorted(((ressemblance(voulu, i.get("name", "")) + (0.15 if art and art in _norm(i.get("artists", "")) else 0), i)
                    for i in await favoris(ctx)), key=lambda x: -x[0])
    if notes and notes[0][0] >= regles.c("musique", {}).get("seuil_titre", 0.75):
        i = notes[0][1]
        await ctx.spotify("play", {"track_uris": [i["uri"]]})
        return ctx.ok(fait=f"« {i['name']} » de {i.get('artists')} (dans les titres likés)")
    res = await ctx.spotify("play_search", {"query": voulu + (" " + args["artiste"] if args.get("artiste") else ""), "type": "track"})
    return ctx.ok(fait="recherche sur tout Spotify (absent des titres likés)", reponse=res[:300])


@outil("jouer_playlist", "Joue une playlist Spotify de Greg d'après son nom, même approximatif.",
       {"nom": {"type": "string", "description": "nom de la playlist tel que Greg le dit"}}, requis=["nom"], necessite=["spotify"])
async def jouer_playlist(ctx, args):
    mots = lambda t: {w for w in _norm(t).replace("-", " ").split() if len(w) > 2}
    pls = await ctx.json("list_playlists", {}, mcp=True)
    voulu = mots(args.get("nom", ""))
    best = max(pls, key=lambda p: (len(voulu & mots(p.get("name", ""))), -len(p.get("name", ""))), default=None)
    if not best or not voulu & mots(best.get("name", "")):
        return ctx.non_fait(f"aucune playlist ne correspond à « {args.get('nom', '')} »", playlists=[p.get("name") for p in pls])
    await ctx.spotify("play", {"context_uri": "spotify:playlist:" + best["id"]})
    return ctx.ok(fait=f"playlist « {best['name']} » ({best.get('tracks')} titres)")


@outil("liker_morceau", "Ajoute le morceau en cours de lecture aux titres likés (favoris) de Greg : « like ce titre », « j'aime ce morceau ».",
       {}, necessite=["spotify"])
async def liker_morceau(ctx, args):
    pb = await ctx.json("get_playback", {}, mcp=True)
    if not pb.get("uri"):
        return ctx.non_fait("aucun morceau en cours")
    await ctx.spotify("save_tracks", {"track_ids": [pb["uri"]]})
    _cache["favoris_t"] = 0
    return ctx.ok(fait=f"« {pb.get('track')} » de {pb.get('artists')} ajouté aux titres likés")


@outil("volume_musique", "Règle le volume de la musique Spotify. « augmente de 10 % » = changement +10 ; « baisse un peu » = changement -10 ; "
       "« mets le volume à 40 » = niveau 40. Ne jamais confondre un changement avec un niveau.",
       {"changement": {"type": "integer", "description": "variation relative en points, positive ou négative"},
        "niveau": {"type": "integer", "minimum": 0, "maximum": 100, "description": "niveau absolu, seulement si Greg donne un niveau"}},
       necessite=["spotify"])
async def volume_musique(ctx, args):
    devs = await ctx.json("get_devices", {}, mcp=True)
    act = next((d for d in devs if isinstance(d, dict) and d.get("is_active")), None)
    if not act:
        return ctx.non_fait("aucune musique en cours sur Spotify")
    cur = act.get("volume_percent")
    if time.time() - _cache["vol_t"] < 60 and _cache["vol_dev"] == act["id"]:
        cur = _cache["vol"]                     # Spotify renvoie le volume avec quelques secondes de retard
    if args.get("changement") not in (None, 0):
        new = (cur or 0) + int(args["changement"])
    elif args.get("niveau") is not None:
        new = int(args["niveau"])
    else:
        return ctx.non_fait("ni changement ni niveau demandé", volume_actuel=cur)
    plafond = regles.c("musique", {}).get("volume_max", 80)   # le CXN100 est en mode préampli : c'est le vrai volume
    demande, new = new, max(0, min(plafond, new))
    await ctx.spotify("set_volume", {"volume_percent": new, "device_id": act["id"]})
    if not ctx.sim:
        _cache.update(vol=new, vol_t=time.time(), vol_dev=act["id"])
    return ctx.ok(fait=f"volume {cur} → {new} %", non_fait=[f"plafonné à {plafond} % (demandé : {demande} %)"] if demande > plafond else [])


# ---------------------------------------------------------------- écran de la TV
@outil("afficher", "Montre quelque chose à l'écran de la TV, à côté de ton visage, QUAND LA VOIX SUFFIT MAL : "
       "une énumération (courses, étapes), un mot à épeler ou un nom propre difficile, un choix numéroté entre "
       "plusieurs possibilités. Tu dis quand même l'essentiel à voix haute : l'écran ne fait que préciser. "
       "N'affiche jamais la simple transcription de ta réponse, et jamais plus de quelques lignes courtes. "
       "Les listes produites par un autre outil (agenda, météo, rappels, musique, CRM) s'affichent toutes seules : "
       "ne les redemande pas ici.",
       {"titre": {"type": "string", "description": "deux ou trois mots au-dessus (ex. « Courses »)"},
        "lignes": {"type": "array", "items": {"type": "string"},
                   "description": "les lignes de l'énumération, très courtes ; numérotées automatiquement"},
        "mot": {"type": "string", "description": "un mot ou un nom à montrer en gros (orthographe)"},
        "epeler": {"type": "boolean", "description": "vrai pour détacher les lettres du mot"},
        "note": {"type": "string", "description": "une ligne discrète en dessous (ex. « dis « le deux » »)"}})
async def afficher(ctx, args):
    lignes = [str(l).strip() for l in (args.get("lignes") or []) if str(l).strip()][:9]
    mot = str(args.get("mot") or "").strip()
    if not lignes and not mot:
        return ctx.non_fait("rien à afficher")
    duree = regles.c("cartes", {}).get("duree_s", 15)
    if lignes:
        carte = {"gabarit": "liste", "titre": str(args.get("titre") or "")[:28], "duree": duree,
                 "items": [{"cle": str(i + 1), "texte": l[:52]} for i, l in enumerate(lignes)]}
    else:
        carte = {"gabarit": "texte", "titre": str(args.get("titre") or "")[:28], "texte": mot[:64],
                 "epeler": bool(args.get("epeler")), "note": str(args.get("note") or "")[:52], "duree": duree}
    if lignes and args.get("note"): carte["items"].append({"cle": "", "texte": str(args["note"])[:52]})
    await ctx.envoyer({"type": "carte", "carte": carte})
    return ctx.ok(fait="affiché à l'écran : " + (", ".join(lignes) if lignes else mot))


# ---------------------------------------------------------------- lieux
async def _domicile(ctx):
    """Coordonnées du domicile, lues une fois chez Home Assistant puis gardées : le plan doit marcher même si HA tombe."""
    fichier = os.path.expanduser("~/kinectface/etat/domicile.json")
    if _cache.get("domicile") is None and os.path.exists(fichier):
        try: _cache["domicile"] = json.load(open(fichier, encoding="utf-8"))
        except (ValueError, OSError): pass
    if _cache.get("domicile") is None and time.time() - _cache.get("domicile_t", 0) > 3600:
        _cache["domicile_t"] = time.time()                # un seul essai par heure : si HA refuse, on n'insiste pas
        try:
            cfg = await ctx.ha("GET", "/api/config")
            if cfg.get("latitude") is not None:
                _cache["domicile"] = {"lat": float(cfg["latitude"]), "lon": float(cfg["longitude"])}
                os.makedirs(os.path.dirname(fichier), exist_ok=True)
                json.dump(_cache["domicile"], open(fichier, "w", encoding="utf-8"))
        except Exception as e:
            log_domicile(e)                               # sans domicile : pas de distance, le reste marche quand même
    d = _cache.get("domicile")
    return (d["lat"], d["lon"]) if d else None


def log_domicile(e):
    import logging
    logging.getLogger("cerveau").warning("coordonnées du domicile indisponibles (Home Assistant) : %s", e)


def _ville_defaut():
    """La ville de référence, telle que réglée dans regles.yaml (« Montréal, Québec » → « Montréal »)."""
    return (regles.c("lieu", {}).get("autour_defaut") or "").split(",")[0].strip()


async def _autour(ctx):
    """(coordonnées servant de biais, est-ce le vrai domicile ?).

    Sans le domicile, une recherche générique part au hasard : « trouve-moi une pharmacie » est tombé au
    Kremlin-Bicêtre. On se rabat donc sur la région par défaut, qui suffit à orienter la recherche même si elle
    ne permet pas de calculer une distance.
    """
    chez = await _domicile(ctx)
    if chez:
        return chez, True
    import asyncio, plan
    if _cache.get("region") is None:
        try:
            r = await asyncio.to_thread(plan.geocoder, regles.c("lieu", {}).get("autour_defaut", "Montréal, Québec"), None)
            _cache["region"] = (r["lat"], r["lon"]) if r else False
        except Exception:
            _cache["region"] = False
    return (_cache["region"] or None), False


@outil("lieu", "Montre un lieu sur un plan à l'écran de la TV : « où est … », « c'est où … », « localise-moi … », "
       "« trouve-moi … », « l'adresse de … », une adresse, un commerce, un restaurant, une ville, un monument. "
       "À utiliser dès que Greg demande où se trouve quelque chose, quelle que soit sa formulation — la voix "
       "sait mal dire une adresse ou une direction. Tu annonces ensuite le nom et la distance à voix haute. "
       "Pour un TRAJET vers ce lieu (« comment y aller », « itinéraire »), utilise plutôt itineraire.",
       {"recherche": {"type": "string", "description": "le lieu tel que Greg le dit (« la Place des Arts », "
                                                       "« une pharmacie », « 12 rue Saint-Denis »)"}},
       requis=["recherche"])
async def lieu(ctx, args):
    import asyncio, plan
    q = str(args.get("recherche") or "").strip()
    if not q:
        return ctx.non_fait("aucun lieu demandé")
    autour, chez_moi = await _autour(ctx)
    try:
        trouve = await asyncio.to_thread(plan.geocoder, q, autour, _ville_defaut())
    except Exception as e:
        return ctx.non_fait(f"le service de localisation n'a pas répondu ({e})")
    if not trouve:
        return ctx.non_fait(f"aucun lieu trouvé pour « {q} »")
    loin = ""
    zoom = regles.c("lieu", {}).get("zoom_defaut", 15)
    if chez_moi:
        km, direction = plan.distance_direction(autour, (trouve["lat"], trouve["lon"]))
        # virgule décimale : la carte est lue par un francophone, pas par un ordinateur
        loin = (f"à {km:.1f} km".replace(".", ",") if km >= 1 else f"à {int(km * 1000)} m") + f" au {direction}"
        zoom = plan.zoom_utile(km)
    # le plan est dessiné en arrière-plan : la carte et la voix partent tout de suite, l'image arrive après
    cle = plan.cle_de(trouve["lat"], trouve["lon"], PLAN_L, PLAN_H, zoom)
    image = regles.c("url_cerveau", "http://192.0.2.31:8802").rstrip("/") + f"/plan/{cle}.png"
    asyncio.get_running_loop().run_in_executor(
        None, lambda: plan.vignette(trouve["lat"], trouve["lon"], PLAN_L, PLAN_H, zoom))
    # « 3637 Rue University » puis « 3637 Rue University, Montréal » : la deuxième ligne n'apprend que la ville
    note = trouve["adresse"]
    if note.startswith(trouve["nom"]):
        note = note[len(trouve["nom"]):].lstrip(" ,") or note
    await ctx.envoyer({"type": "carte", "carte": {
        "gabarit": "lieu", "titre": trouve["categorie"][:24], "texte": trouve["nom"][:44],
        "note": note[:60], "accent": loin, "image": image,
        "duree": regles.c("lieu", {}).get("duree_s", 25)}})
    return ctx.ok(fait=f"{trouve['nom']} ({trouve['adresse']}) montré sur le plan" + (f", {loin}" if loin else ""),
                  nom=trouve["nom"], adresse=trouve["adresse"], distance=loin)


PLAN_L, PLAN_H = 704, 416        # EXACTEMENT la place du plan sur la TV (la carte prend deux tiers de l'écran) :
                                 # dessiné à sa taille d'affichage, il n'est ni redimensionné ni adouci
MAISON = re.compile(r"\b(la |ma |notre |chez )?(maison|moi|nous|ici|domicile|appart|chez-moi)\b")


def _est_la_maison(texte):
    """« depuis la maison », « de chez moi » : le point de départ par défaut, surtout pas une adresse à chercher.

    Géocodé littéralement, « ma maison » tombait sur un lieu au hasard et l'itinéraire partait de là — 3,3 km au
    sud là où la même demande sans départ donnait 633 m au nord-est (21/09).
    """
    return bool(MAISON.search(_norm(texte or "")))


MODES = {"voiture": "driving", "auto": "driving", "transport": "transit", "bus": "transit", "metro": "transit",
         "marche": "walking", "pied": "walking", "velo": "bicycling", "vélo": "bicycling"}


@outil("itineraire", "Itinéraire vers un lieu : affiche un QR code sur la TV — Greg le scanne et le trajet s'ouvre dans "
       "son téléphone — et peut aussi le lui envoyer en notification. Pour « comment aller à … », « itinéraire pour … », "
       "« envoie-moi le trajet / l'adresse de … sur mon téléphone ». Pour seulement situer un lieu, utilise lieu.",
       {"destination": {"type": "string", "description": "le lieu d'arrivée, tel que Greg le dit"},
        "depart": {"type": "string", "description": "seulement si Greg donne un point de départ autre que la maison"},
        "mode": {"type": "string", "enum": sorted(set(MODES)), "description": "seulement si Greg le précise"},
        "envoyer_telephone": {"type": "boolean", "description": "true si Greg demande de l'envoyer sur son téléphone"}},
       requis=["destination"])
async def itineraire(ctx, args):
    import asyncio, urllib.parse, plan
    but = str(args.get("destination") or "").strip()
    if not but:
        return ctx.non_fait("aucune destination demandée")
    autour, chez_moi = await _autour(ctx)
    de = "" if _est_la_maison(args.get("depart")) else str(args.get("depart") or "").strip()
    try:
        arrivee = await asyncio.to_thread(plan.geocoder, but, autour, _ville_defaut())
        depart = await asyncio.to_thread(plan.geocoder, de, autour, _ville_defaut()) if de else None
    except Exception as e:
        return ctx.non_fait(f"le service de localisation n'a pas répondu ({e})")
    if not arrivee:
        return ctx.non_fait(f"aucun lieu trouvé pour « {but} »")
    origine = (depart["lat"], depart["lon"]) if depart else (autour if chez_moi else None)
    p = {"api": "1", "destination": f"{arrivee['lat']},{arrivee['lon']}",
         "travelmode": MODES.get(_norm(args.get("mode") or ""), "driving")}
    if origine: p["origin"] = f"{origine[0]},{origine[1]}"
    lien = "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(p)
    loin = ""
    if origine:
        km, direction = plan.distance_direction(origine, (arrivee["lat"], arrivee["lon"]))
        loin = (f"{km:.1f} km".replace(".", ",") if km >= 1 else f"{int(km * 1000)} m") + f" au {direction}"
    _, cle = await asyncio.to_thread(plan.qr, lien)
    envoye = ""
    if args.get("envoyer_telephone"):
        # le lien part dans « lien », pas collé au texte : sans ça, toucher la notification n'ouvrait que le
        # tableau de bord de Home Assistant au lieu de l'itinéraire (retour de Greg, 21/09)
        r = await ctx.json("notifier_telephone", {"message": f"{arrivee['nom']} — {arrivee['adresse']}",
                                                  "titre": "Itinéraire", "lien": lien})
        envoye = ", envoyé sur le téléphone" if r.get("ok") else ""
    await ctx.envoyer({"type": "carte", "carte": {
        "gabarit": "lieu", "titre": "itinéraire", "texte": arrivee["nom"][:44], "note": arrivee["adresse"][:60],
        "accent": (loin + " · " if loin else "") + "scanne le QR code",
        "image": regles.c("url_cerveau", "http://192.0.2.31:8802").rstrip("/") + f"/plan/{cle}.png",
        "duree": regles.c("lieu", {}).get("duree_s", 25)}})
    return ctx.ok(fait=f"itinéraire vers {arrivee['nom']} ({arrivee['adresse']}) en QR code sur la TV"
                       + (f", {loin}" if loin else "") + envoye,
                  nom=arrivee["nom"], adresse=arrivee["adresse"], distance=loin)


# ---------------------------------------------------------------- réglages à la voix
# Seulement des valeurs SÛRES, lues à chaud par le cerveau. Pas de consignes : une phrase de prompt mal tournée
# peut rendre Bulle muette (vu le 20/09), ça ne se règle pas à la voix sans le banc de tests derrière.
# Pas non plus la section « nuit » ni « client » : le Pi les lit dans sa copie, qui n'est mise à jour qu'au déploiement.
REGLAGES = {
    "duree des cartes": {"chemin": ["cartes", "duree_s"], "min": 5, "max": 120, "unite": "secondes",
                         "quoi": "combien de temps une carte reste affichée"},
    "duree des plans": {"chemin": ["lieu", "duree_s"], "min": 5, "max": 120, "unite": "secondes",
                        "quoi": "combien de temps un plan reste affiché"},
    "volume maximum": {"chemin": ["musique", "volume_max"], "min": 10, "max": 100, "unite": "%",
                       "quoi": "le volume le plus fort que la musique peut atteindre"},
    "luminosite du panneau led": {"chemin": ["club", "luminosite_max"], "min": 10, "max": 100, "unite": "%",
                                  "quoi": "la luminosité maximale des dalles en mode club"},
    "memoire de conversation": {"chemin": ["historique_tours"], "min": 0, "max": 20, "unite": "échanges",
                                "quoi": "combien d'échanges tu gardes en tête"},
    "oubli": {"chemin": ["oubli_apres_s"], "min": 60, "max": 3600, "unite": "secondes",
              "quoi": "au bout de combien de temps tu oublies la conversation"},
    "cartes": {"chemin": ["cartes", "actives"], "booleen": True, "quoi": "l'affichage des cartes sur la TV"},
}


MOTS_VIDES = {"le", "la", "les", "de", "des", "du", "un", "une", "a", "au", "aux", "en", "et", "mon", "ma", "mes",
              "ce", "cette", "pour", "sur", "que", "qui", "est"}


def _trouver_reglage(demande, booleen=None):
    """Le réglage dont le NOM est le mieux couvert par la demande.

    On compte la proportion du nom reconnue, pas le nombre de mots communs : sinon « coupe les cartes » gagne sur
    « durée des cartes » à égalité. Les mots vides sont ignorés, sans quoi « change la couleur du visage » tombait
    sur « luminosité DU panneau led ». Le type de la valeur (nombre ou oui/non) départage le reste.
    """
    mots = {m for m in _norm(demande).split() if m not in MOTS_VIDES and len(m) > 2}
    best, score_max = None, 0.0
    for nom, d in REGLAGES.items():
        if booleen is not None and bool(d.get("booleen")) is not booleen: continue
        cles = {m for m in _norm(nom).split() if m not in MOTS_VIDES and len(m) > 2}
        # par préfixe, sinon « oublie » ne reconnaît pas le réglage « oubli »
        commun = {c for c in cles if any(m == c or (len(m) > 3 and (m.startswith(c) or c.startswith(m))) for m in mots)}
        score = len(commun) / max(1, len(cles))
        if commun and score > score_max: best, score_max = nom, score
    return best


VRAI = ("oui", "actif", "active", "allume", "vrai", "on", "remets")
FAUX = ("non", "inactif", "desactive", "coupe", "faux", "off", "arrete")


def _valeur_yaml(txt, booleen=False):
    t = _norm(txt)
    if booleen:
        # On compare des MOTS ENTIERS (par préfixe, pour attraper « allumées » ou « désactivé »). En cherchant la
        # sous-chaîne, « non » contenait « on » et « désactive » contenait « active » : un refus était lu comme un
        # accord, et l'interrupteur « cartes » de la page de suivi ne pouvait pas être éteint.
        mots = re.findall(r"[a-z0-9]+", t)
        if any(m.startswith(v) for m in mots for v in VRAI): return True
        if any(m.startswith(f) for m in mots for f in FAUX): return False
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", t)
    return None if not m else (int(float(m.group(0).replace(",", "."))))


def _ecrire_regle(chemin, valeur, auteur="voix", nom=None):
    """Remplace UNE valeur dans config/regles.yaml sans toucher aux commentaires (yaml.safe_dump les perdrait).

    Gère les deux styles présents dans le fichier : bloc indenté (cartes:\n  duree_s: 15) et accolades sur une
    ligne (lieu: {zoom_defaut: 15, duree_s: 25}).
    """
    import subprocess, yaml
    depot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fichier = os.path.join(depot, "config", "regles.yaml")
    avant = open(fichier, encoding="utf-8").read()
    lignes = avant.split("\n")
    rendu = "true" if valeur is True else "false" if valeur is False else str(valeur)

    def remplace_ligne(i, cle):
        m = re.match(r"^(\s*" + re.escape(cle) + r":\s*)(.*?)(\s+#.*)?$", lignes[i])
        if not m: return False
        lignes[i] = m.group(1) + rendu + (m.group(3) or "")
        return True

    i = next((k for k, l in enumerate(lignes) if l.startswith("cerveau:")), None)
    if i is None: raise RuntimeError("section « cerveau » introuvable")
    if len(chemin) == 1:
        cible = next((k for k in range(i + 1, len(lignes)) if re.match(r"^  " + re.escape(chemin[0]) + r":", lignes[k])), None)
        if cible is None or not remplace_ligne(cible, chemin[0]): raise RuntimeError(f"réglage introuvable : {chemin}")
    else:
        parent = next((k for k in range(i + 1, len(lignes)) if re.match(r"^  " + re.escape(chemin[0]) + r":", lignes[k])), None)
        if parent is None: raise RuntimeError(f"section introuvable : {chemin[0]}")
        if "{" in lignes[parent]:                      # style accolades : on remplace dans la ligne
            neuf, n = re.subn(r"(\b" + re.escape(chemin[1]) + r":\s*)([^,}]+)", lambda m: m.group(1) + rendu, lignes[parent], count=1)
            if not n: raise RuntimeError(f"réglage introuvable : {chemin}")
            lignes[parent] = neuf
        else:
            cible = None
            for k in range(parent + 1, len(lignes)):
                if lignes[k].strip() and not lignes[k].startswith("    "): break   # fin du bloc du parent
                if re.match(r"^    " + re.escape(chemin[1]) + r":", lignes[k]):
                    cible = k; break
            if cible is None or not remplace_ligne(cible, chemin[1]): raise RuntimeError(f"réglage introuvable : {chemin}")
    apres = "\n".join(lignes)
    open(fichier, "w", encoding="utf-8", newline="\n").write(apres)
    try:                                               # le fichier doit rester lisible, sinon on remet l'ancien
        lu = yaml.safe_load(apres)["cerveau"]
        for c in chemin: lu = lu[c]
        assert str(lu).lower() == rendu.lower(), f"écrit {rendu}, relu {lu}"
    except Exception as e:
        open(fichier, "w", encoding="utf-8", newline="\n").write(avant)
        raise RuntimeError(f"écriture annulée : {e}")
    # commit : sinon la prochaine fusion de l'agent de nuit écrase le réglage sans bruit
    subprocess.run(["git", "add", "config/regles.yaml"], cwd=depot, capture_output=True, timeout=20)
    subprocess.run(["git", "commit", "-m", f"Réglage ({auteur}) : {'.'.join(chemin)} = {rendu}"],
                   cwd=depot, capture_output=True, timeout=20)
    import journal
    journal.action(auteur, "reglage", f"{nom or '.'.join(chemin)} → {rendu}")


@outil("reglage", "Change un réglage de Bulle quand Greg le demande à voix haute (« garde les cartes trente secondes », "
       "« mets le volume maximum à 70 », « coupe les cartes »). Sans valeur, dit le réglage actuel. "
       "Ne sert QUE pour la liste de réglages connue : durée des cartes, durée des plans, volume maximum, luminosité "
       "du panneau LED, mémoire de conversation, oubli, cartes actives ou non. Pour tout le reste, dis que tu ne sais "
       "pas encore le régler.",
       {"reglage": {"type": "string", "description": "le réglage, tel que Greg le nomme"},
        "valeur": {"type": "string", "description": "la nouvelle valeur (nombre, ou oui/non) ; absente = lecture"}},
       requis=["reglage"])
async def reglage(ctx, args):
    demande = str(args.get("reglage") or "") + " " + str(args.get("valeur") or "")
    brut = str(args.get("valeur") or "")
    attendu = None                                  # nombre ou oui/non : ça départage « coupe les cartes » de leur durée
    if brut.strip():
        attendu = False if re.search(r"\d", brut) else (True if _valeur_yaml(brut, True) is not None else None)
    nom = _trouver_reglage(demande, attendu)
    if not nom:
        return ctx.non_fait("je ne sais pas encore régler ça", connus=list(REGLAGES))
    d = REGLAGES[nom]
    actuel = regles.c(d["chemin"][0], {})
    actuel = actuel.get(d["chemin"][1]) if len(d["chemin"]) > 1 and isinstance(actuel, dict) else actuel
    if args.get("valeur") in (None, ""):
        return ctx.ok(fait=f"{nom} : {actuel} {d.get('unite', '')}".strip(), reglage=nom, valeur=actuel)
    v = _valeur_yaml(str(args["valeur"]), d.get("booleen"))
    if v is None:
        return ctx.non_fait(f"je n'ai pas compris la valeur pour {nom}")
    if not d.get("booleen") and not (d["min"] <= v <= d["max"]):
        return ctx.non_fait(f"{nom} doit être entre {d['min']} et {d['max']} {d.get('unite', '')}".strip(), valeur_actuelle=actuel)
    if ctx.sim:
        return ctx.ok(fait=f"{nom} : {actuel} → {v} (simulé)", reglage=nom, valeur=v)
    try:
        _ecrire_regle(d["chemin"], v, auteur="voix", nom=nom)
    except Exception as e:
        return ctx.non_fait(f"je n'ai pas pu enregistrer le réglage ({e})")
    return ctx.ok(fait=f"{nom} : {actuel} → {v} {d.get('unite', '')}".strip(), reglage=nom, valeur=v)


# ---------------------------------------------------------------- mémoire longue
# Le seul endroit où de la parole survit aux durées de vie du journal. D'où la voie étroite : Bulle ne retient
# QUE ce que Greg lui demande de retenir, jamais ce qu'elle déduit d'une conversation. C'est ce qui permet de
# garder la règle de vie privée intacte — un souvenir est un choix, pris à voix haute, effaçable à voix haute.
@outil("retenir", "Mémorise durablement un fait, SEULEMENT quand Greg le demande explicitement (« retiens que… », "
       "« souviens-toi que… », « n'oublie pas que… », « note que… »). N'appelle JAMAIS cet outil de toi-même, même "
       "si quelque chose te semble important à garder : ce qui se dit dans le salon ne se conserve pas sans qu'on "
       "l'ait demandé. Reformule le fait à la troisième personne, court et autonome, tel qu'il sera relu dans six "
       "mois (« Greg est allergique aux arachides », pas « il est allergique »).",
       {"fait": {"type": "string", "description": "le fait à retenir, une phrase courte et autonome"}},
       requis=["fait"])
async def retenir(ctx, args):
    fait = str(args.get("fait") or "").strip()
    if len(fait) < 3:
        return ctx.non_fait("je n'ai pas compris quoi retenir")
    if ctx.sim:
        return ctx.ok(fait=f"retenu : {fait} (simulé)", souvenir=fait)
    journal.retenir(fait)
    return ctx.ok(fait=f"c'est retenu : {fait}", souvenir=fait)


@outil("oublier", "Efface un fait retenu, quand Greg le demande (« oublie que… », « ce n'est plus vrai », "
       "« tu peux oublier… »). Donne dans `quoi` les mots de Greg : le souvenir le plus proche est retrouvé "
       "tout seul. Si rien ne correspond, dis-le plutôt que d'effacer autre chose.",
       {"quoi": {"type": "string", "description": "le souvenir à effacer, tel que Greg le désigne"}},
       requis=["quoi"])
async def oublier(ctx, args):
    quoi = str(args.get("quoi") or "").strip()
    faits = journal.souvenirs()
    if not faits:
        return ctx.non_fait("je ne retiens rien pour l'instant")
    seuil = (regles.c("memoire", {}) or {}).get("seuil_oubli", 0.5)
    meilleur = max(faits, key=lambda f: ressemblance(quoi, f["fait"]))
    if ressemblance(quoi, meilleur["fait"]) < seuil:
        # Effacer « le plus proche » quand rien ne ressemble, c'est effacer au hasard un souvenir que Greg
        # tenait à garder — et un souvenir effacé ne revient pas. On préfère ne rien faire et le dire.
        return ctx.non_fait("je ne vois pas de quel souvenir tu parles", souvenirs=[f["fait"] for f in faits][:5])
    if ctx.sim:
        return ctx.ok(fait=f"oublié : {meilleur['fait']} (simulé)", souvenir=meilleur["fait"])
    journal.oublier(meilleur["id"])
    return ctx.ok(fait=f"c'est oublié : {meilleur['fait']}", souvenir=meilleur["fait"])


@outil("lister_souvenirs", "Dit ce que Bulle a retenu sur demande de Greg (« qu'est-ce que tu sais de moi ? », "
       "« qu'est-ce que tu as retenu ? », « qu'est-ce que tu te rappelles ? »). La liste s'affiche sur la TV : "
       "dis le nombre et les deux ou trois qui comptent, sans tout réciter.", {})
async def lister_souvenirs(ctx, args):
    faits = journal.souvenirs()
    if not faits:
        return ctx.ok(fait="je ne retiens rien pour l'instant", souvenirs=[], total=0)
    return ctx.ok(fait=f"{len(faits)} souvenirs", souvenirs=[f["fait"] for f in faits], total=len(faits))
