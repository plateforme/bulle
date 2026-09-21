"""Page de suivi de Bulle (/suivi) : santé, à tester, fait, en attente de revue, incidents, échanges récents.

Protégée par le jeton partagé (regles.jeton). Elle laisse changer des réglages qui sont ÉCRITS dans
config/regles.yaml et committés : elle n'est pas moins sensible que le WebSocket.
"""
import datetime, glob, html, json, os, subprocess, time, urllib.parse

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import journal
import outils_composes as oc
import regles

RAPPORTS = os.path.expanduser("~/kinectface/etat/rapports")
DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _h(t):
    return html.escape(str(t if t is not None else ""))


def _date(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else ""


def _git_log(n=15):
    try:
        out = subprocess.run(["git", "log", f"-{n}", "--format=%h|%an|%ar|%s"], cwd=DEPOT, capture_output=True, text=True, timeout=10).stdout
        return [l.split("|", 3) for l in out.splitlines() if l.count("|") >= 3]
    except Exception:
        return []


def _sante():
    fichiers = sorted(glob.glob(os.path.join(RAPPORTS, "*.json")))
    if not fichiers: return None, None
    d = json.load(open(fichiers[-1], encoding="utf-8"))
    return d.get("sante"), d.get("nuit")


CSS = """
:root{--bg:#fafaf8;--fg:#1b1b1a;--mut:#6b6b66;--card:#fff;--line:#e6e4df;--acc:#ff6a4d;--ok:#2f8f5b;--ko:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#111110;--fg:#ecebe6;--mut:#9a9890;--card:#1b1b19;--line:#2c2b28}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:860px;margin:0 auto;padding:18px 16px 60px}h1{font-size:22px;margin:4px 0 2px}h1 span{color:var(--acc)}
.sub{color:var(--mut);margin-bottom:18px}h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:26px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0}
.row{display:flex;gap:10px;align-items:flex-start;justify-content:space-between}.mut{color:var(--mut);font-size:13px}
.pill{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);margin:2px 4px 2px 0}
.ok{color:var(--ok);border-color:var(--ok)}.ko{color:var(--ko);border-color:var(--ko)}.acc{color:var(--acc);border-color:var(--acc)}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer}
button.ok{border-color:var(--ok)}button.ko{border-color:var(--ko)}input[type=text],input[type=number],input[type=password],select{font:inherit;width:100%;margin-top:6px;padding:6px 8px;
border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}code{font-size:13px}details summary{cursor:pointer;color:var(--mut)}
.mini{width:96px !important;margin-top:0 !important}.rg{align-items:center;flex-wrap:wrap}
.phrase{font-size:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
"""


COOKIE = "bulle_jeton"


def _entre(requete):
    """Le jeton, pris dans le cookie (navigateur) ou dans l'en-tête Authorization (curl, scripts)."""
    return (regles.jeton_valide(requete.cookies.get(COOKIE))
            or regles.jeton_valide(requete.headers.get("authorization")))


def _connexion(erreur=""):
    return HTMLResponse(
        f"<!doctype html><html lang=fr><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Suivi de Bulle</title><style>{CSS}</style><main><h1>Suivi de <span>Bulle</span></h1>"
        f"<div class=sub>Cette page pilote les réglages de Bulle : elle demande le jeton.</div>"
        + (f"<div class='card ko'>{_h(erreur)}</div>" if erreur else "")
        + "<form method=post action=/suivi/connexion class=card>"
          "<label>Jeton<input type=password name=jeton autofocus autocomplete=current-password></label>"
          "<div style='margin-top:10px'><button>Entrer</button></div></form></main></html>",
        status_code=401)


def monter(app):
    @app.get("/suivi/connexion", response_class=HTMLResponse)
    def connexion():
        return _connexion()

    @app.post("/suivi/connexion")
    def ouvrir(jeton: str = Form("")):
        if not regles.jeton_valide(jeton):
            return _connexion("Jeton refusé.")
        r = RedirectResponse("/suivi", status_code=303)
        # httponly : un script de page ne doit pas pouvoir relire le jeton. samesite=lax : pas d'envoi depuis un
        # site tiers. Pas de secure= : la page est servie en clair sur le réseau local, il n'y a pas de TLS ici.
        r.set_cookie(COOKIE, jeton.strip(), httponly=True, samesite="lax", max_age=30 * 86400)
        return r

    @app.get("/suivi", response_class=HTMLResponse)
    def page(requete: Request, msg: str = ""):
        if not _entre(requete):
            return _connexion()
        s, nuit = _sante()
        tests = journal.lire("select * from a_tester order by (statut='a_faire') desc, id desc limit 30")
        attente = journal.lire("select * from actions where type='proposition' and statut='en_attente' order by id desc")
        faits = journal.lire("select * from actions where statut in ('applique','fait') and type in ('revue','exploitation','correction','deploiement') "
                             "order by id desc limit 25")
        refus = journal.lire("select * from actions where statut='refuse' and type='revue' order by id desc limit 10")
        incidents = journal.lire("select * from incidents where nuit = (select max(nuit) from incidents) order by gravite, id")
        echanges = journal.lire("select * from echanges where simulation=0 order by id desc limit 25")
        retours = journal.lire("select count(*) n from retours where statut='nouveau'")[0]["n"]
        o = [f"<!doctype html><html lang=fr><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Suivi de Bulle</title><style>{CSS}</style><main><h1>Suivi de <span>Bulle</span></h1>"
             f"<div class=sub>Mis à jour {time.strftime('%d/%m %H:%M')} · dernière analyse : {_h(nuit or '—')} · {retours} retour(s) de Greg non traités</div>"]
        # santé
        o.append("<h2>Santé</h2><div class=grid>")
        if s:
            svc = {**s.get("services_vm", {}), **s.get("services_pi", {})}
            ko = [k for k, v in svc.items() if v != "active"]
            su = s.get("suivi") or {}
            cartes = [("Services", "tous actifs" if not ko else "arrêtés : " + ", ".join(ko), not ko),
                      # le pic des 24 h, pas l'instantané : l'analyse tourne à 3 h, quand le Pi est froid
                      ("Pi", (f"{su['temperature_max_c']} °C au pic · bridé {su.get('bride_part', 0):.0%} du temps"
                              if su.get("temperature_max_c") is not None
                              else f"{s.get('pi_temperature', '?')} °C · {s.get('pi_throttled', '?')}"),
                       (su.get("temperature_max_c") or s.get("pi_temperature") or 0) < 78 and (su.get("bride_part") or 0) <= 0.02),
                      ("Kinect", f"inclinaison {su.get('inclinaison_deg', '?')}° · {su.get('bascules_presence', '?')} bascules", abs(su.get("inclinaison_deg") or 0) <= 8),
                      ("Outils", f"{s.get('cerveau_outils', '?')} outils", isinstance(s.get("cerveau_outils"), int))]
            for t, v, bon in cartes:
                o.append(f"<div class=card><div class=mut>{t}</div><div class='{'ok' if bon else 'ko'}'>{_h(v)}</div></div>")
        else:
            o.append("<div class=card>Pas encore d'analyse.</div>")
        o.append("</div>")
        # réglages (même écriture que l'outil vocal : config/regles.yaml, commit inclus)
        if msg: o.append(f"<div class='card acc'>{_h(msg)}</div>")
        o.append("<h2>Réglages</h2>")
        for nom, d in oc.REGLAGES.items():
            val = regles.c(d["chemin"][0], {})
            if len(d["chemin"]) > 1 and isinstance(val, dict): val = val.get(d["chemin"][1])
            if d.get("booleen"):
                champ = ("<select name=valeur class=mini>"
                         f"<option value=oui{' selected' if val else ''}>oui</option>"
                         f"<option value=non{'' if val else ' selected'}>non</option></select>")
                bornes = "oui ou non"
            else:
                champ = f"<input type=number class=mini name=valeur value='{_h(val)}' min={d['min']} max={d['max']}>"
                bornes = f"{d['min']} à {d['max']} {d.get('unite', '')}".strip()
            o.append(f"<div class=card><form method=post action=/suivi/reglage class='row rg'>"
                     f"<div><b>{_h(nom)}</b><div class=mut>{_h(d['quoi'])} · {_h(bornes)}</div></div>"
                     f"<div class=row style='gap:6px;align-items:center'><input type=hidden name=nom value='{_h(nom)}'>"
                     f"{champ}<button>Enregistrer</button></div></form></div>")
        o.append("<div class='card mut'>Les consignes données au modèle ne sont pas modifiables ici : elles se valident "
                 "au banc de tests. Les réglages du Pi (mode nuit, micro) demandent un déploiement.</div>")
        # à tester
        o.append("<h2>À tester</h2>")
        for t in tests:
            if t["statut"] == "a_faire":
                o.append(f"<div class=card><div class=phrase>🎙️ « {_h(t['phrase'])} »</div><div class=mut>attendu : {_h(t['attendu'])} · {_h(t['origine'])} · {_date(t['ts'])}</div>"
                         f"<form method=post action='/suivi/test/{t['id']}'><input type=text name=commentaire placeholder='commentaire (facultatif)'>"
                         f"<div class=row style='margin-top:8px;justify-content:flex-start'><button class=ok name=statut value=ok>✓ Ça marche</button>"
                         f"<button class=ko name=statut value=ko>✗ Ça ne marche pas</button></div></form></div>")
            else:
                o.append(f"<div class=card><span class='pill {'ok' if t['statut']=='ok' else 'ko'}'>{'✓' if t['statut']=='ok' else '✗'}</span>"
                         f"« {_h(t['phrase'])} » <span class=mut>{_h(t['commentaire'] or '')}</span></div>")
        if not tests: o.append("<div class='card mut'>Rien à tester pour l'instant.</div>")
        # en attente de revue
        o.append("<h2>En attente de revue (tech lead)</h2>")
        for a in attente:
            d = json.loads(a["detail"] or "{}")
            o.append(f"<div class=card><div class=row><b>{_h(a['titre'])}</b><span class='pill acc'>{_h(d.get('verdict'))}</span></div>"
                     f"<div class=mut>{_h(a['commit_ref'])} · {_date(a['ts'])}</div><details><summary>rapport de l'agent</summary>"
                     f"<pre style='white-space:pre-wrap'>{_h(d.get('rapport'))}</pre></details></div>")
        if not attente: o.append("<div class='card mut'>Aucune proposition en attente.</div>")
        # fait
        o.append("<h2>Fait récemment</h2>")
        for a in faits:
            o.append(f"<div class=card><span class='pill ok'>{_h(a['auteur'])}</span>{_h(a['titre'])} <span class=mut>· {_date(a['ts'])} {_h(a['commit_ref'] or '')}</span></div>")
        o.append("<details class=card><summary>Historique du code (git)</summary>" + "".join(
            f"<div class=mut><code>{_h(h)}</code> {_h(s_)} — {_h(au)}, {_h(q)}</div>" for h, au, q, s_ in _git_log()) + "</details>")
        if refus:
            o.append("<h2>Refusé</h2>")
            for a in refus:
                d = json.loads(a["detail"] or "{}")
                o.append(f"<div class=card><span class='pill ko'>refusé</span>{_h(a['titre'])}<div class=mut>{_h(d.get('raison'))}</div></div>")
        # incidents
        o.append(f"<h2>Incidents de la dernière analyse ({len(incidents)})</h2>")
        for i in incidents:
            d = json.loads(i["detail"] or "{}")
            o.append(f"<div class=card><span class='pill {'ko' if i['gravite']=='haute' else ''}'>{_h(i['gravite'])}</span><b>{_h(i['type'])}</b> ×{d.get('occurrences', 1)} "
                     f"<span class='pill'>{_h(i['statut'])}</span><div class=mut>{_h(i['resume'])}</div></div>")
        # échanges
        o.append("<h2>Derniers échanges</h2>")
        for e in echanges:
            outils = ", ".join(x.get("nom", "") for x in json.loads(e["outils"] or "[]"))
            if e["ignore_raison"]:
                o.append(f"<div class='card mut'>{_date(e['ts'])} · ignoré ({_h(e['ignore_raison'])}) : « {_h(e['brut'])} »</div>")
            else:
                o.append(f"<div class=card><div class=mut>{_date(e['ts'])} · {e['duree'] or 0:.1f} s {('· ' + _h(outils)) if outils else ''}</div>"
                         f"<div>🗣️ « {_h(e['texte'])} »</div><div>🫧 {_h(e['reponse'])}</div></div>")
        o.append("</main></html>")
        return "".join(o)

    @app.post("/suivi/reglage")
    def enregistrer_reglage(requete: Request, nom: str = Form(...), valeur: str = Form(...)):
        if not _entre(requete):
            return _connexion("Jeton expiré ou absent : ce réglage n'a pas été enregistré.")
        d = oc.REGLAGES.get(nom)
        if not d:
            return RedirectResponse("/suivi?msg=Réglage inconnu.", status_code=303)
        v = oc._valeur_yaml(valeur, d.get("booleen"))
        if v is None:
            msg = f"Valeur incomprise pour « {nom} »."
        elif not d.get("booleen") and not (d["min"] <= v <= d["max"]):
            msg = f"« {nom} » doit être entre {d['min']} et {d['max']}."
        else:
            try:
                oc._ecrire_regle(d["chemin"], v, auteur="page de suivi", nom=nom)
                msg = f"« {nom} » réglé sur {v}."
            except Exception as e:
                msg = f"Échec : {e}"
        return RedirectResponse("/suivi?msg=" + urllib.parse.quote(msg), status_code=303)

    @app.post("/suivi/test/{tid}")
    def noter_test(tid: int, requete: Request, statut: str = Form(...), commentaire: str = Form("")):
        if not _entre(requete):
            return _connexion("Jeton expiré ou absent : rien n'a été noté.")
        if statut in ("ok", "ko"):
            journal.ecrire("update a_tester set statut = ?, commentaire = ? where id = ?", (statut, commentaire[:500], tid))
            if statut == "ko":   # un test raté devient un retour de Greg, traité par l'agent la nuit suivante
                t = journal.lire("select * from a_tester where id = ?", (tid,))[0]
                journal.ecrire("insert into retours(ts, echange_id, phrase) values(?,?,?)",
                               (time.time(), None, f"test raté : « {t['phrase']} » (attendu : {t['attendu']}) {commentaire}".strip()))
        return RedirectResponse("/suivi", status_code=303)
