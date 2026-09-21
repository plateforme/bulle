"""Analyste de Bulle (sans IA) : journaux du Pi + du cerveau + SQLite + bilans de santé → incidents classés et rapport.

  python agent/analyste.py                 analyse des dernières 24 h
  python agent/analyste.py --heures 6
Écrit les incidents dans SQLite (table incidents) et un rapport dans ~/kinectface/etat/rapports/AAAA-MM-JJ.md.
Chaque incident est rapproché des fiches de connaissances/pannes.yaml.
"""
import argparse, datetime, difflib, json, os, re, subprocess, sys, time, unicodedata

import yaml

ICI = os.path.dirname(os.path.abspath(__file__))
DEPOT = os.path.dirname(ICI)
sys.path.insert(0, os.path.join(DEPOT, "cerveau"))
import journal  # noqa: E402
import regles   # noqa: E402

PI = os.environ.get("BULLE_PI", "plateforme@192.0.2.6")
LED = os.environ.get("BULLE_LED", "plateforme@192.0.2.26")
SSH = ["ssh", "-i", os.path.expanduser("~/.ssh/id_bulle_deploy"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
RAPPORTS = os.path.expanduser("~/kinectface/etat/rapports")


def sh(cmd, distant=False, timeout=60):
    try:
        r = subprocess.run((SSH + [PI, cmd]) if distant else ["bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception as e:
        return f"__erreur__ {e}"


def norm(t):
    return "".join(c for c in unicodedata.normalize("NFD", str(t).lower()) if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------- santé
def sante():
    s = {"ts": round(time.time())}
    s["services_vm"] = {u: sh(f"systemctl is-active {u}").strip() for u in
                        ("kinectface-cerveau", "ollama", "stt-gateway", "kyutai-tts", "spotify-mcp", "maison-tool")}
    pi = sh("systemctl is-active kinectface-face kinectface-compagnon kinectface-tracker | paste -sd' '; vcgencmd measure_temp; "
            "vcgencmd get_throttled; cat ~/kinectface/etat/suivi.json 2>/dev/null; echo; uptime -p", distant=True)
    lignes = pi.splitlines()
    try:
        etats = lignes[0].split()
        s["services_pi"] = dict(zip(("kinectface-face", "kinectface-compagnon", "kinectface-tracker"), etats))
        s["pi_temperature"] = float(re.search(r"temp=([\d.]+)", pi).group(1))
        s["pi_throttled"] = re.search(r"throttled=(0x[0-9a-f]+)", pi).group(1)
        m = re.search(r"(\{.*\})", pi)
        s["suivi"] = json.loads(m.group(1)) if m else {}
    except Exception:
        s["pi_injoignable"] = pi[:300]
    try:
        import urllib.request
        h = json.load(urllib.request.urlopen("http://localhost:8802/health", timeout=10))
        s["cerveau_outils"] = len(h.get("tools", []))
    except Exception as e:
        s["cerveau_outils"] = f"erreur {e}"
    s["led"] = _sante_led()
    return s


def _ha(entite):
    """État d'une entité Home Assistant (jeton lu dans mcp.env, comme les serveurs d'outils)."""
    import urllib.request
    conf = {}
    for l in open(os.path.expanduser("~/.config/opencode/mcp.env"), encoding="utf-8"):
        if "=" in l and not l.strip().startswith("#"):
            k, v = l.strip().split("=", 1)
            conf[k] = re.split(r"\s+#", v, 1)[0].strip().strip('"').strip("'")
    r = urllib.request.Request(conf.get("HA_URL", "http://192.0.2.15:8123").rstrip("/") + "/api/states/" + entite,
                               headers={"Authorization": "Bearer " + conf.get("HA_TOKEN", "")})
    return json.load(urllib.request.urlopen(r, timeout=10))


def _sante_led():
    """Panneau LED : le mode club est-il demandé, et le panneau anime-t-il vraiment ?"""
    try:
        club = _ha("input_boolean.led_club")["state"] == "on"
        st, tempo = _ha("sensor.led_club_style_en_cours"), _ha("sensor.led_club_tempo")
        # le capteur « tempo » est republié chaque minute tant que le mode club anime : c'est le battement de cœur
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(tempo["last_updated"].replace("Z", "+00:00"))).total_seconds() / 60
        return {"club_on": club, "style_en_cours": st["state"], "anime_depuis_min": round(age, 1)}
    except Exception as e:
        return {"erreur": str(e)[:120]}


def _premier_proche(brut, nom):
    """Repli pour les lignes d'avant la correction « vie privée » : même règle que le cerveau, à la lecture."""
    if not brut: return None
    premier = norm(re.split(r"[\s,.!?]+", brut.strip() + " ")[0])
    ok = len(premier) >= 4 and premier != nom and difflib.SequenceMatcher(None, premier, nom).ratio() >= 0.7
    return premier if ok else None


# ---------------------------------------------------------------- incidents
def incidents_depuis(depuis, s):
    inc = []

    def ajoute(type_, gravite, cle, resume, **detail):
        inc.append({"type": type_, "gravite": gravite, "cle": cle, "resume": resume, "detail": detail})

    ech = journal.lire("select * from echanges where ts >= ? and simulation = 0 order by ts", (depuis,))
    nom = norm(regles.c("nom", "Bulle"))
    precedent_fin = None
    for e in ech:
        outils = json.loads(e["outils"] or "[]")
        for o in outils:
            if o.get("nom") == "__reponse_vide__":
                ajoute("reponse_vide", "moyenne", "reponse_vide", f"réponse vide pour « {e['texte']} »", echange=e["id"])
                continue
            if o.get("demande") and o.get("demande") != o.get("nom"):
                ajoute("outil_redirige", "faible", "redirige:" + o["demande"], f"{o['demande']} redirigé vers {o['nom']}", echange=e["id"])
            if not o.get("ok", True):
                motif = "outil inconnu" if "outil inconnu" in (o.get("resultat") or "") else "erreur"
                ajoute("outil_inconnu" if motif == "outil inconnu" else "outil_erreur", "haute" if motif == "erreur" else "moyenne",
                       f"{motif}:{o.get('nom')}", f"{o.get('nom')} : {(o.get('resultat') or '')[:120]}", echange=e["id"],
                       phrase=e["texte"], args=o.get("args"))
        rep = norm(e.get("reponse") or "")
        # « ça ne marche pas, il faudra corriger » est un RETOUR sur l'échange précédent, pas une capacité manquante :
        # l'agent y a passé deux nuits (deux chantiers de 40 min) à vouloir implémenter ce que Greg n'avait pas demandé.
        est_retour = any(norm(x) in norm(e["texte"] or "") for x in regles.c("retour_negatif", []))
        if e["ignore_raison"] is None and not est_retour and re.search(r"ne sais pas|ne peux pas|n'ai pas pu|pas trouve", rep):
            ajoute("capacite_manquante", "moyenne", "capacite:" + norm(e["texte"])[:40], f"« {e['texte']} » → « {e['reponse'][:90]} »",
                   echange=e["id"], outils=[o.get("nom") for o in outils])
        if (e.get("duree") or 0) > 12:
            ajoute("latence", "faible", "latence", f"{e['duree']:.0f} s pour « {e['texte']} »", echange=e["id"])
        # Un énoncé ignoré ne garde plus son texte (vie privée) : la longueur et, le cas échéant, un premier mot
        # proche du nom. Les lignes d'avant cette correction portent encore « brut » — on sait les lire aussi,
        # le temps que la purge les emporte.
        longueur = e["longueur"] if e["longueur"] is not None else len(e["brut"] or "")
        if e["ignore_raison"] == "hallucination":
            ajoute("hallucination_stt", "faible", "hallucination",
                   f"transcription fantôme ignorée ({longueur} caractères, motif {e['ignore_detail'] or '?'})", echange=e["id"])
        if e["ignore_raison"] == "pas_nomme":
            premier = e["premier_mot"] or _premier_proche(e["brut"], nom)
            if premier and longueur < 80:
                ajoute("ignore_nom_probable", "moyenne", "nom:" + premier,
                       f"ignoré, commence par « {premier} » ({longueur} caractères)", echange=e["id"])
            elif precedent_fin and 0 < e["ts"] - precedent_fin < 30:
                ajoute("ignore_juste_apres_reponse", "moyenne", "fenetre",
                       f"ignoré {e['ts'] - precedent_fin:.0f} s après une réponse ({longueur} caractères)", echange=e["id"])
        else:
            precedent_fin = e["ts"] + (e.get("duree") or 0)
    for r in journal.lire("select r.*, e.texte, e.reponse, e.outils from retours r left join echanges e on e.id = r.echange_id "
                          "where r.ts >= ? and r.statut = 'nouveau'", (depuis,)):
        ajoute("retour_negatif", "haute", f"retour:{r['id']}", f"Greg : « {r['phrase']} » après « {r['texte']} » → « {(r['reponse'] or '')[:80]} »",
               retour=r["id"], echange=r["echange_id"], outils=json.loads(r["outils"] or "[]"))

    # journaux du Pi (client, suivi) et du cerveau
    since = datetime.datetime.fromtimestamp(depuis).strftime("%Y-%m-%d %H:%M:%S")
    jpi = sh(f"journalctl -u kinectface-compagnon -u kinectface-tracker -u kinectface-face --since '{since}' --no-pager -o cat", distant=True, timeout=120)
    jvm = sh(f"journalctl -u kinectface-cerveau --since '{since}' --no-pager -o cat", timeout=120)
    probas = [float(x) for x in re.findall(r"parole ([0-9.]+)", jpi)]
    faibles, fortes = sum(0.15 <= p < regles.get("client", "vad_seuil_on", 0.3) for p in probas), sum(p >= 0.5 for p in probas)
    if probas and faibles > max(10, fortes * 0.25):
        ajoute("parole_sous_seuil", "moyenne", "vad", f"{faibles} s de parole probable sous le seuil (contre {fortes} s de parole nette)",
               faibles=faibles, fortes=fortes)
    bascules = len(re.findall(r"quelqu'un est là", jpi))
    heures = max(1.0, (time.time() - depuis) / 3600)
    if bascules / heures > 15:
        ajoute("presence_instable", "moyenne", "presence", f"{bascules} arrivées annoncées en {heures:.0f} h", par_heure=round(bascules / heures, 1))
    for motif, type_, grav in ((r"Input control transfer failed|zero plane info|const shift", "kinect_init", "moyenne"),
                               (r"gspca_main: kinect", "gspca", "haute"), (r"Traceback", "exception", "haute"),
                               (r"Scheduled restart job", "redemarrage_service", "moyenne")):
        n = len(re.findall(motif, jpi + jvm))
        if n: ajoute(type_, grav, type_, f"{n} occurrence(s) de « {motif.split('|')[0]} » dans les journaux", occurrences=n)
    # santé
    su = s.get("suivi") or {}
    if su.get("inclinaison_deg") is not None and abs(su["inclinaison_deg"]) > 8:
        ajoute("kinect_inclinee", "moyenne", "inclinaison", f"Kinect inclinée de {su['inclinaison_deg']}°")
    # On juge sur le MAXIMUM des 24 h relevé par le suivi (pi/tracker.py), pas sur l'instantané : la boucle de
    # nuit interroge le Pi à 3 h, quand il est froid depuis des heures, et n'a donc JAMAIS rien vu — alors qu'en
    # journée il tourne bridé (mesuré le 21/09 : 76 °C au repos, throttled=0x20002). Les bits hauts de
    # « throttled » sont collants depuis le démarrage : ils ne disent pas ce qui se passe maintenant.
    pic, bride = su.get("temperature_max_c"), su.get("bride_part") or 0
    t_max = regles.get("suivi", "pi_temperature_max", 82)
    b_max = regles.get("suivi", "pi_bride_part_max", 0.02)
    if pic is not None:
        # Le bridage d'abord : c'est le seul signal qui dit que la machine perd vraiment quelque chose. La
        # température ne sert qu'à prévenir avant d'y arriver, et son seuil est calé au-dessus de la vie normale
        # du Pi (73-80 °C mesurés le 21/09) — sinon l'incident tomberait tous les jours et ne voudrait plus rien.
        if bride > b_max or pic > t_max:
            ajoute("pi_chauffe", "moyenne", "pi_chauffe",
                   f"Pi monté à {pic} °C (il y a {su.get('temperature_max_il_y_a_min', '?')} min), "
                   f"bridé {bride:.0%} du temps sur {su.get('fenetre_h', '?')} h",
                   pic=pic, bride_part=bride, fenetre_h=su.get("fenetre_h"))
    elif s.get("pi_temperature", 0) > 78 or s.get("pi_throttled", "0x0") not in ("0x0", "0x20000", "0x80000", "0xa0000"):
        # repli : le suivi n'a pas encore été redéployé, ou il est arrêté
        ajoute("pi_chauffe", "moyenne", "pi_chauffe", f"Pi à {s.get('pi_temperature')} °C, throttled={s.get('pi_throttled')}")
    led = s.get("led") or {}
    if led.get("club_on") and (led.get("anime_depuis_min") or 0) > 5:
        ajoute("club_sans_capteur", "moyenne", "club_sans_capteur",
               f"mode club demandé mais le panneau n'anime plus (dernier signe de vie il y a {led['anime_depuis_min']:.0f} min)")
    for k, v in {**s.get("services_vm", {}), **s.get("services_pi", {})}.items():
        if v != "active": ajoute("service_arrete", "haute", "service:" + k, f"service {k} : {v}")
    return inc


def rapprocher(inc):
    fiches = yaml.safe_load(open(os.path.join(DEPOT, "connaissances", "pannes.yaml"), encoding="utf-8"))["pannes"]
    corr = {"kinect_inclinee": "kinect-mal-inclinee", "pi_chauffe": "pi-chauffe", "kinect_init": "libfreenect-ancien",
            "gspca": "gspca-kinect", "club_sans_capteur": "club-capteur-mort"}
    for i in inc:
        ids = [f["id"] for f in fiches if i["type"] in (f.get("detection", {}).get("incident") or [])]
        if i["type"] in corr: ids.append(corr[i["type"]])
        if i["type"] == "outil_erreur" and re.search(r"is not a function|not valid JSON|Unexpected (non-whitespace|token)", i["resume"]):
            ids.append("spotify-mcp-bug")
        i["pannes"] = sorted(set(ids))
    return inc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heures", type=float, default=24)
    ap.add_argument("--sans-ecriture", action="store_true")
    a = ap.parse_args()
    depuis = time.time() - a.heures * 3600
    s = sante()
    inc = rapprocher(incidents_depuis(depuis, s))
    # regroupement : une ligne par clé, avec le nombre d'occurrences
    groupes = {}
    for i in inc:
        g = groupes.setdefault(i["cle"], dict(i, occurrences=0, exemples=[]))
        g["occurrences"] += 1
        if len(g["exemples"]) < 3: g["exemples"].append(i["resume"])
    ordre = {"haute": 0, "moyenne": 1, "faible": 2}
    liste = sorted(groupes.values(), key=lambda g: (ordre[g["gravite"]], -g["occurrences"]))
    nuit = datetime.date.today().isoformat()
    stats = journal.lire("select count(*) n, sum(ignore_raison is null) repondus, sum(ignore_raison = 'pas_nomme') ignores, "
                         "avg(case when ignore_raison is null then duree end) duree from echanges where ts >= ? and simulation = 0", (depuis,))[0]
    if not a.sans_ecriture:
        for g in liste:
            journal.ecrire("insert into incidents(ts, nuit, type, gravite, cle, resume, detail) values(?,?,?,?,?,?,?)",
                           (time.time(), nuit, g["type"], g["gravite"], g["cle"], g["resume"],
                            json.dumps({"occurrences": g["occurrences"], "exemples": g["exemples"], "pannes": g["pannes"], **g["detail"]}, ensure_ascii=False)))
    os.makedirs(RAPPORTS, exist_ok=True)
    md = [f"# Rapport de Bulle — {nuit}", "",
          f"Échanges : {stats['n'] or 0} ({stats['repondus'] or 0} répondus, {stats['ignores'] or 0} ignorés), "
          f"durée moyenne de réponse {stats['duree'] or 0:.1f} s.", "",
          "## Santé", "```", json.dumps(s, ensure_ascii=False, indent=1), "```", "", f"## Incidents ({len(liste)})"]
    for g in liste:
        md.append(f"- **[{g['gravite']}] {g['type']}** ×{g['occurrences']} — {g['resume']}" + (f" (fiches : {', '.join(g['pannes'])})" if g["pannes"] else ""))
    chemin = os.path.join(RAPPORTS, f"{nuit}.md")
    if not a.sans_ecriture:
        open(chemin, "w", encoding="utf-8").write("\n".join(md) + "\n")
    print("\n".join(md))
    json.dump({"nuit": nuit, "sante": s, "incidents": liste, "stats": stats},
              open(os.path.join(RAPPORTS, f"{nuit}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
