"""Cartes : ce que la voix dit mal (listes, chiffres, orthographe) est envoyé à l'écran de la TV.

Choix de conception : la carte est le rendu d'un RÉSULTAT D'OUTIL, pas du texte du LLM. Le modèle ne décide ni de
la mise en page ni du contenu — donc pas d'hallucination de carte, pas de latence ajoutée, et ça marche même quand
il bâcle sa phrase. Seule exception : l'outil « afficher » (outils_composes.py), pour ce qu'aucun outil ne produit
(épeler un nom, montrer un choix numéroté…).

La carte part au moment du résultat d'outil, donc pendant que Bulle « réfléchit » : elle occupe les 3-6 s de
silence avant la première phrase, au lieu de les laisser vides.

Le cerveau envoie des données (voir carte.py sur le Pi pour les gabarits), jamais une image : le Pi pourra
remettre en page selon la distance mesurée par la Kinect sans rien redemander.
"""
import datetime
import json
import re

import regles

CIEL = {"ciel dégagé": "dégagé", "principalement dégagé": "dégagé", "partiellement nuageux": "nuageux"}
MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
# Twenty passe par un outil unique (twenty_execute_tool) : le vrai objet est dans toolName.
TWENTY = {"tasks": "Tâches", "people": "Contacts", "companies": "Entreprises", "notes": "Notes",
          "opportunities": "Opportunités", "calendar_events": "Agenda", "messages": "Messages"}


def _f(v, suffixe=""):
    """-4.0 → « -4 » : à l'écran, les décimales ne servent qu'à encombrer."""
    try: return f"{round(float(v)):d}{suffixe}"
    except (TypeError, ValueError): return str(v or "") + suffixe


def _jour(iso):
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
        return ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"][d.weekday()]
    except ValueError:
        return str(iso)


def _jour_court(iso):
    """« 2026-09-28T07:00:00.000Z » → « 28 sept. » (l'heure d'une échéance ne veut rien dire)."""
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
    except (ValueError, TypeError):
        return ""
    return f"{d.day} {MOIS[d.month - 1]}"


def _nom_record(r):
    """Le libellé d'une fiche Twenty : title, subject, ou un nom composite {firstName, lastName}."""
    for cle in ("title", "subject", "name"):
        v = r.get(cle)
        if isinstance(v, dict):
            v = " ".join(x for x in (v.get("firstName"), v.get("lastName")) if x)
        if v: return str(v)
    return ""


def _heure(txt):
    """« 2026-09-19T14:00 » ou « 14:00 » → « 14 h 00 » ; sinon rien (événement sur la journée)."""
    m = re.search(r"(\d{1,2})[:h](\d{2})", str(txt or ""))
    return f"{int(m.group(1))} h {m.group(2)}" if m else ""


def _liste(titre, items, duree=None):
    return {"gabarit": "liste", "titre": titre, "items": items} | ({"duree": duree} if duree else {})


def _evenements(evs, titre):
    items = []
    for e in evs:
        h = _heure(e.get("debut") or e.get("heure") or e.get("date_debut") or "")
        items.append({"cle": h or "—", "texte": str(e.get("titre") or e.get("resume") or e.get("nom") or "")[:60],
                      "passe": bool(e.get("passe"))})
    # Le seul point corail d'une carte d'agenda se pose sur le prochain événement (refonte graphique du 21/09).
    # Le choix est fait ici parce qu'il est déterministe : l'ordre et le « passé » viennent de l'outil, pas du
    # Pi, qui ne sait pas quelle heure il est chez le cerveau.
    prochain = next((i for i in items if not i["passe"]), None)
    if prochain is not None:
        prochain["prochain"] = True
    return _liste(titre, items) if items else None


def depuis_outil(nom, args, resultat):
    """Retourne les données d'une carte, ou None si cet outil n'a rien à montrer."""
    try:
        d = json.loads(resultat)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("ok") is False:
        return None

    if nom == "meteo_actuelle":
        items = [{"cle": "maintenant", "valeur": _f(d.get("temperature_c"), "°"),
                  "note": CIEL.get(d.get("ciel"), d.get("ciel"))},
                 {"cle": "ressenti", "valeur": _f(d.get("ressenti_c"), "°")},
                 {"cle": "vent", "valeur": _f(d.get("vent_kmh"), " km/h")}]
        return {"gabarit": "cles", "titre": d.get("ville", "Météo"), "items": items}

    if nom == "previsions":
        jours = (d.get("jours") or [])[:3]
        if not jours: return None
        return {"gabarit": "cles", "titre": d.get("ville", "Prévisions"),
                "items": [{"cle": _jour(j.get("date")), "valeur": _f(j.get("max_c"), "°"),
                           "note": CIEL.get(j.get("ciel"), j.get("ciel"))} for j in jours]}

    if nom in ("agenda_du_jour", "prochains_evenements"):
        return _evenements(d.get("evenements") or [], d.get("date", "Aujourd'hui") if nom == "agenda_du_jour" else "À venir")

    if nom == "lister_rappels":
        r = d.get("rappels") or []
        return _liste("Rappels", [{"cle": "·", "texte": str(x.get("titre") or x.get("nom") or x)[:60]} for x in r]) if r else None

    if nom == "lister_souvenirs":
        # Un souvenir se relit, il ne se récite pas : la liste à l'écran, le nombre et deux ou trois à la voix.
        faits = d.get("souvenirs") or []
        return _liste(f"Ce que je retiens · {d.get('total', len(faits))}",
                      [{"cle": "·", "texte": str(f)[:60]} for f in faits[:9]]) if faits else None

    if nom in ("derniers_courriels", "rechercher_courriels"):
        msgs = (d.get("courriels") or d.get("messages") or [])[:6]
        return _liste("Courriels", [{"cle": "·", "texte": f"{str(m.get('de') or m.get('expediteur') or '')[:22]} — "
                                                          f"{str(m.get('sujet') or '')[:44]}"} for m in msgs]) if msgs else None

    if nom == "etat_maison":
        allumees = [x.strip() for x in d.get("lumieres_allumees") or []]
        lect = next((l for l in d.get("lecteurs") or [] if l.get("etat") == "playing"), None)
        items = [{"cle": "lumières allumées", "valeur": str(len(allumees))}]
        if d.get("meteo"): items.append({"cle": "dehors", "valeur": _f(d["meteo"].get("temperature"), "°")})
        if lect: items.append({"cle": lect.get("nom", "musique"), "valeur": str(lect.get("titre") or "")[:28]})
        return {"gabarit": "cles", "titre": "La maison", "items": items}

    if nom in ("get_playback", "jouer_morceau", "play_search", "jouer_playlist", "jouer_favoris", "liker_morceau"):
        t = d.get("titre") or d.get("track") or (d.get("item") or {}).get("name")
        if not t: return None
        artiste = d.get("artiste") or d.get("artist") or ""
        album = d.get("album") or ""
        carte = {"gabarit": "media", "texte": str(t)[:44], "duree": 0,      # reste tant que ça joue
                 "note": " · ".join(x for x in (str(artiste)[:30], str(album)[:30]) if x),
                 "accent": d.get("appareil") or d.get("device") or ""}
        if d.get("pochette") or d.get("image"): carte["image"] = d.get("pochette") or d.get("image")
        return carte

    if nom == "twenty_execute_tool":
        reel = str((args or {}).get("toolName") or "")
        if not reel.startswith("find_"):       # les catalogues et schémas d'outils ne s'affichent pas
            return None
        res = d.get("result") or {}
        items = []
        for r in res.get("records") or []:
            texte = _nom_record(r)
            if texte:
                items.append({"cle": _jour_court(r.get("dueAt") or r.get("date") or r.get("createdAt")) or "·",
                              "texte": texte[:52], "passe": str(r.get("status") or "").upper() == "DONE"})
        if not items: return None
        titre = TWENTY.get(re.sub(r"^find_(many|one)_", "", reel), "CRM")
        n = str(res.get("count") or len(items))
        return _liste(titre if len(items) == 1 else f"{titre} · {n}", items)

    if nom in ("server_status", "gpu_usage_by_person"):
        plats = [(k, v) for k, v in d.items() if isinstance(v, (int, float, str)) and not isinstance(v, bool)][:5]
        return {"gabarit": "cles", "titre": nom.replace("_", " "),
                "items": [{"cle": k.replace("_", " ")[:22], "valeur": str(v)[:14]} for k, v in plats]} if plats else None

    return None


def autorisee(nom):
    """Un outil peut être exclu des cartes par config/regles.yaml (réglage à chaud, sans redéploiement)."""
    r = regles.c("cartes", {}) or {}
    return bool(r.get("actives", True)) and nom not in (r.get("exclues") or [])
