"""Boucle nocturne de Bulle (100 % locale) : analyse → choix des incidents → agent OpenCode en branche → validation → revue.

  python agent/boucle.py                       nuit complète (minuterie systemd, 3 h)
  python agent/boucle.py --max 1 --essai       un seul incident, sans écrire l'état des incidents (mise au point)
L'agent ne fusionne JAMAIS : chaque correction validée devient une branche agent/… « prête pour revue » que le tech lead
(revue.py) fusionne ou refuse. Seules les commandes d'exploitation sûres (inclinaison de la Kinect) sont appliquées ici.
"""
import argparse, datetime, json, os, re, shutil, signal, subprocess, sys, time

import yaml

ICI = os.path.dirname(os.path.abspath(__file__))
DEPOT = os.path.dirname(ICI)
sys.path.insert(0, os.path.join(DEPOT, "cerveau"))
sys.path.insert(0, os.path.join(DEPOT, "tests"))
import journal  # noqa: E402
import regles   # noqa: E402  (durées de vie du journal)
import banc as juge  # noqa: E402  (taux par cas et règle « régression vs instable »)

CHANTIERS = os.path.expanduser("~/kinectface/chantiers")
VENV_PY = os.path.expanduser("~/kinectface/venv/bin/python")
UVICORN = os.path.expanduser("~/kinectface/venv/bin/uvicorn")
OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")
MODELE = os.environ.get("BULLE_AGENT_MODELE", "ollama/qwen3.8:27b")
PORT_TEST = 8813          # (8803 = reranker d'Ollama)
DUREE_CHANTIER = 1200     # le 20/09, deux chantiers ont tenu les 40 min de la limite précédente et la nuit a fini à 9 h 46
# Le LLM n'est pas déterministe : jouer chaque cas une seule fois, c'est tirer le verdict au sort. Mais tripler
# toute la liste ferait passer un chantier de ~24 à ~32 min et la nuit déborderait. On ne rejoue donc que les
# ÉCHECS (jusqu'à 3 essais) : eux seuls font rejeter une correction — voir banc.comparer et banc.FRANC.
# Ce que le modèle de l'agent réclame sur la carte, contexte compris (qwen3.8:27b = 17,7 Go). En dessous,
# Ollama en met une partie sur le processeur et un chantier de 20 min n'a aucune chance d'aboutir.
VRAM_AGENT_MIO = int(os.environ.get("BULLE_VRAM_AGENT_MIO", "19000"))
REPETITIONS = int(os.environ.get("BULLE_BANC_REPETITIONS", "1"))
CONFIRMER = int(os.environ.get("BULLE_BANC_CONFIRMER", "2"))
ZONES_DEFAUT = ["config/regles.yaml", "cerveau/outils_composes.py", "tests/banc.yaml", "connaissances/pannes.yaml", "RAPPORT_AGENT.md"]
TRAITABLES = {"retour_negatif", "capacite_manquante", "reponse_vide", "ignore_nom_probable", "ignore_juste_apres_reponse",
              "parole_sous_seuil", "presence_instable", "outil_inconnu", "outil_erreur", "outil_redirige"}
SSH_BASE = ["ssh", "-i", os.path.expanduser("~/.ssh/id_bulle_deploy"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
PI_SSH = SSH_BASE + [os.environ.get("BULLE_PI", "plateforme@192.0.2.6")]
LED_SSH = SSH_BASE + [os.environ.get("BULLE_LED", "plateforme@192.0.2.26")]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def run(cmd, cwd=None, timeout=600, env=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def git(*a, cwd=DEPOT):
    return run(["git", *a], cwd=cwd)


def slug(t):
    t = re.sub(r"[^a-z0-9]+", "-", t.lower())
    return t.strip("-")[:40] or "incident"


# ---------------------------------------------------------------- choix des incidents
def a_traiter(nuit, maxi):
    deja = {r["cle"] for r in journal.lire("select cle from incidents where statut in ('propose','corrige','ignore') and ts > ?",
                                           (time.time() - 7 * 86400,))}
    cands = journal.lire("select * from incidents where nuit = ? and statut = 'ouvert' order by id", (nuit,))
    choisis = []
    for c in cands:
        if c["type"] not in TRAITABLES or c["cle"] in deja: continue
        d = json.loads(c["detail"] or "{}")
        if c["type"] == "outil_redirige" and d.get("occurrences", 1) < 3: continue     # déjà rattrapé ; seulement si fréquent
        choisis.append((c, d))
    prio = {"retour_negatif": 0, "outil_erreur": 1, "capacite_manquante": 2, "reponse_vide": 3}
    choisis.sort(key=lambda x: prio.get(x[0]["type"], 5))
    return choisis[:maxi]


def fiches_pour(pannes_ids):
    tout = yaml.safe_load(open(os.path.join(DEPOT, "connaissances", "pannes.yaml"), encoding="utf-8"))["pannes"]
    return [f for f in tout if f["id"] in pannes_ids]


def details_echanges(detail):
    ids = [v for k, v in detail.items() if k in ("echange",) and v]
    lignes = []
    for i in ids:
        for e in journal.lire("select * from echanges where id = ?", (i,)):
            lignes.append(f"- Greg (transcription brute) : « {e['brut']} »\n  texte compris : « {e['texte']} »\n"
                          f"  outils appelés : {e['outils']}\n  réponse de Bulle : « {e['reponse']} »")
    return "\n".join(lignes)


# ---------------------------------------------------------------- instance de test du cerveau
class CerveauTest:
    def __init__(self, dossier):
        self.dossier, self.p = dossier, None

    def __enter__(self):
        env = dict(os.environ, BULLE_DB=os.path.expanduser("~/kinectface/etat/banc.db"),
                   BULLE_REGLES=os.path.join(self.dossier, "config", "regles.yaml"))
        env.update(_env_fichier(os.path.expanduser("~/.config/opencode/mcp.env")))
        self.p = subprocess.Popen([UVICORN, "server:app", "--host", "127.0.0.1", "--port", str(PORT_TEST)],
                                  cwd=os.path.join(self.dossier, "cerveau"), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        for _ in range(40):
            time.sleep(1)
            if self.p.poll() is not None:
                raise RuntimeError(f"l'instance de test s'est arrêtée (code {self.p.returncode}) : port {PORT_TEST} occupé ?")
            rc, out = run(["curl", "-sf", f"http://127.0.0.1:{PORT_TEST}/health"], timeout=30)
            # on vérifie que c'est bien NOTRE instance, chargée avec les règles de la branche
            if rc == 0 and self.dossier in out: return self
        raise RuntimeError("l'instance de test du cerveau ne démarre pas")

    def __exit__(self, *a):
        if self.p:
            os.killpg(self.p.pid, signal.SIGTERM)
            try: self.p.wait(10)
            except Exception: os.killpg(self.p.pid, signal.SIGKILL)


def _env_fichier(chemin):
    env = {}
    try:
        for l in open(chemin, encoding="utf-8"):
            l = l.strip()
            if l and not l.startswith("#") and "=" in l:
                k, v = l.split("=", 1)
                env[k.strip()] = re.split(r"\s+#", v, 1)[0].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def controles(dossier):
    """Les contrôles de la CI, rejoués sur le chantier → message d'échec, ou None si tout passe.

    Ils prennent quelques secondes et voient ce que le banc ne peut pas voir : une clé de cas mal orthographiée
    (« interdit » pour « interdits ») passe le banc sans plus rien vérifier, et un outil à effet glissé en
    lecture seule s'exécuterait POUR DE VRAI au test suivant. Les passer AVANT le banc évite en prime de payer
    vingt minutes de LLM pour une correction qui ne tient pas debout.
    """
    for titre, cmd in (("fichiers YAML", [VENV_PY, os.path.join(dossier, "outils", "valider.py")]),
                       ("tests unitaires", [VENV_PY, "-m", "pytest"])):
        rc, out = run(cmd, cwd=dossier, timeout=300)
        if rc:
            return f"{titre} : {out.strip()[-400:]}"
    return None


def banc(dossier, url, sortie, filtre=None, repetitions=None):
    cmd = [VENV_PY, os.path.join(dossier, "tests", "banc.py"), "--url", url, "--json", sortie,
           "--repetitions", str(repetitions or REPETITIONS), "--confirmer", str(CONFIRMER)]
    if filtre: cmd += ["--filtre", filtre]
    run(cmd, cwd=dossier, timeout=1800)
    try: return json.load(open(sortie, encoding="utf-8"))
    except Exception: return {"reussis": 0, "total": 0, "cas": []}


# ---------------------------------------------------------------- un chantier
def _vram_libre():
    """Mio libres sur la carte, 0 si on ne sait pas dire."""
    rc, out = run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"])
    try: return int(out.strip().splitlines()[0])
    except (ValueError, IndexError): return 0


def chantier(inc, detail, nuit, reference):
    nom_branche = f"agent/{nuit.replace('-', '')}-{slug(inc['type'] + '-' + inc['cle'])}"
    dossier = os.path.join(CHANTIERS, nom_branche.replace("/", "_"))
    shutil.rmtree(dossier, ignore_errors=True)
    git("worktree", "prune")
    rc, out = git("worktree", "add", "-B", nom_branche, dossier, "main")
    if rc: raise RuntimeError(out)
    # l'agent peut éditer, mais ni lancer de commandes ni aller sur le web
    json.dump({"$schema": "https://opencode.ai/config.json", "permission": {"edit": "allow", "bash": "deny", "webfetch": "deny"}},
              open(os.path.join(dossier, "opencode.json"), "w"))
    exclude = os.path.join(git("rev-parse", "--git-common-dir")[1].strip(), "info", "exclude")
    open(exclude if os.path.isabs(exclude) else os.path.join(DEPOT, exclude), "a").write("\nopencode.json\n")
    fiches = fiches_pour(detail.get("pannes", []))
    zones = sorted(set(ZONES_DEFAUT) | {z for f in fiches for z in f.get("zone", []) if "/" in z})
    refus = journal.lire("select titre, detail from actions where type = 'revue' and statut = 'refuse' and detail like ?",
                         (f"%{inc['cle']}%",))
    consigne = open(os.path.join(ICI, "consigne_agent.md"), encoding="utf-8").read().format(
        zones="\n".join(f"- `{z}`" for z in zones),
        incident=f"type : {inc['type']} (gravité {inc['gravite']}, {detail.get('occurrences', 1)} occurrence(s))\n"
                 f"résumé : {inc['resume']}\nexemples : {detail.get('exemples')}\n\n{details_echanges(detail)}",
        fiches=yaml.safe_dump(fiches, allow_unicode=True, sort_keys=False) if fiches else "(aucune fiche : problème nouveau)",
        refus="\n".join(f"- {r['titre']} : {r['detail']}" for r in refus) or "(aucun)")
    log(f"agent sur {nom_branche} ({MODELE})")
    t0 = time.time()
    rc, sortie_agent = run([OPENCODE, "run", "--auto", "-m", MODELE, "--dir", dossier, consigne], cwd=dossier, timeout=DUREE_CHANTIER)
    log(f"agent terminé en {time.time() - t0:.0f} s (code {rc})")
    # --- validation par l'orchestrateur (le juge n'est pas le LLM)
    verdict, raisons = "pret_pour_revue", []
    rc, diff_noms = git("status", "--porcelain", cwd=dossier)
    modifies = [l[3:].strip() for l in diff_noms.splitlines() if l.strip() and not l.endswith("opencode.json")]
    # les serveurs MCP d'OpenCode déposent leurs traces dans le chantier (.playwright-mcp/) : ce n'est pas du travail
    # de l'agent, ça ne doit pas faire rejeter sa correction
    rebuts = [f for f in modifies if f.startswith(".")]
    if rebuts:
        log(f"ignorés (traces d'outils) : {rebuts}")
        modifies = [f for f in modifies if f not in rebuts]
    hors_zone = [f for f in modifies if f not in zones]
    if hors_zone:
        verdict = "rejete"; raisons.append(f"fichiers hors zone : {hors_zone}")
    if "RAPPORT_AGENT.md" not in modifies:
        raisons.append("pas de RAPPORT_AGENT.md")
    code_modifie = [f for f in modifies if f != "RAPPORT_AGENT.md"]
    for f in code_modifie:
        p = os.path.join(dossier, f)
        try:
            if f.endswith((".yaml", ".yml")): yaml.safe_load(open(p, encoding="utf-8"))
            if f.endswith(".py"): compile(open(p, encoding="utf-8").read(), f, "exec")
        except Exception as e:
            verdict = "rejete"; raisons.append(f"{f} invalide : {e}")
    if verdict != "rejete" and code_modifie:
        echec = controles(dossier)
        if echec:
            verdict = "rejete"; raisons.append(echec)
    resultat_banc = None
    if verdict != "rejete" and code_modifie:
        try:
            with CerveauTest(dossier):
                resultat_banc = banc(dossier, f"ws://127.0.0.1:{PORT_TEST}/ws", os.path.join(dossier, ".banc.json"))
            regressions, instables = juge.comparer(reference, resultat_banc)
            nouveaux = [c for c in resultat_banc["cas"] if c["phrase"] not in {x["phrase"] for x in reference["cas"]}]
            if regressions:
                verdict = "rejete"; raisons.append(f"régressions : {regressions}")
            if instables:
                # ce n'est PAS la faute de la correction : on ne rejette pas, on le dit (le cas de test est à revoir)
                raisons.append(f"cas instables, ignorés dans le verdict : {instables}")
                journal.action("agent", "note", f"cas de test instable : {instables[0][:60]}",
                               json.dumps({"cas": instables, "branche": nom_branche}, ensure_ascii=False), statut="instable")
            if not nouveaux:
                raisons.append("aucun cas de test ajouté")
            else:
                faibles = [c["phrase"] for c in nouveaux if juge.taux(c) < juge.FRANC]
                if faibles:
                    verdict = "rejete"; raisons.append(f"nouveau cas non réussi franchement : {faibles}")
        except Exception as e:
            verdict = "rejete"; raisons.append(f"banc impossible : {e}")
    elif not code_modifie:
        verdict = "diagnostic_seul"
    # --- commit dans la branche + trace
    git("add", "-A", ":!opencode.json", cwd=dossier)
    resume = f"{inc['type']} : {inc['resume'][:80]}"
    git("-c", "user.name=Bulle (agent local)", "-c", "user.email=bulle-agent@local", "commit", "-q", "-m",
        f"[agent] {resume}\n\nVerdict de l'orchestrateur : {verdict}\n" + "\n".join(f"- {r}" for r in raisons), cwd=dossier)
    rapport = ""
    try: rapport = open(os.path.join(dossier, "RAPPORT_AGENT.md"), encoding="utf-8").read()
    except FileNotFoundError: pass
    json.dump({"branche": nom_branche, "verdict": verdict, "raisons": raisons, "incident": inc, "banc": resultat_banc and
               {"reussis": resultat_banc["reussis"], "total": resultat_banc["total"]}, "fichiers": modifies,
               "sortie_agent": sortie_agent[-4000:]}, open(os.path.join(CHANTIERS, nom_branche.replace("/", "_") + ".json"), "w"),
              ensure_ascii=False, indent=1)
    journal.action("agent", "proposition", resume, json.dumps({"branche": nom_branche, "verdict": verdict, "raisons": raisons,
                   "cle": inc["cle"], "rapport": rapport[:3000]}, ensure_ascii=False), commit_ref=nom_branche,
                   statut="en_attente" if verdict in ("pret_pour_revue", "diagnostic_seul") else "rejete_auto")
    journal.ecrire("update incidents set statut = ? where id = ?", ("propose" if verdict != "rejete" else "ouvert", inc["id"]))
    git("worktree", "remove", "--force", dossier)
    log(f"{nom_branche} → {verdict} {raisons}")
    return verdict


# ---------------------------------------------------------------- exploitation sûre
def exploitation(nuit):
    for inc in journal.lire("select * from incidents where nuit = ? and type = 'kinect_inclinee' and statut = 'ouvert'", (nuit,)):
        rc, out = run(PI_SSH + ["python3 -c \"import socket,json; socket.socket(socket.AF_INET,socket.SOCK_DGRAM)"
                                ".sendto(json.dumps({'inclinaison':0}).encode(),('127.0.0.1',5007))\""])
        journal.action("agent", "exploitation", "Kinect remise à l'horizontale", inc["resume"])
        journal.ecrire("update incidents set statut = 'corrige' where id = ?", (inc["id"],))
        log("Kinect remise à l'horizontale")
    for inc in journal.lire("select * from incidents where nuit = ? and type = 'club_sans_capteur' and statut = 'ouvert'", (nuit,)):
        # le fil de lecture du capteur piézo est mort : un redémarrage de l'afficheur suffit (fiche club-capteur-mort)
        rc, out = run(LED_SSH + ["sudo systemctl restart afficheur && sleep 20 && "
                                 "journalctl -u afficheur --since '1 min ago' --no-pager -o cat | grep -c 'mode club'"], timeout=120)
        ok = rc == 0 and out.strip().splitlines()[-1:] != ["0"]
        journal.action("agent", "exploitation", "afficheur LED redémarré (mode club sans capteur)",
                       inc["resume"] + (" → mode club reparti" if ok else " → toujours en panne"), statut="fait" if ok else "rejete_auto")
        journal.ecrire("update incidents set statut = ? where id = ?", ("corrige" if ok else "ouvert", inc["id"]))
        log("afficheur LED redémarré :", "ok" if ok else "toujours en panne")


def entretien():
    """Entretien du journal, avant l'analyse — quand personne ne parle à Bulle.

    L'ordre compte, et il n'est pas intuitif :

    1. effacer le texte des énoncés qui n'étaient pas pour Bulle. AVANT la sauvegarde, sinon la copie du jour
       emporte exactement ce qu'on cherche à faire disparaître (constaté le 21/09 à la première exécution).
    2. sauvegarder. Ce qui reste mérite d'être protégé.
    3. purger selon les durées de vie. APRÈS la sauvegarde : une durée de vie mal réglée doit rester réparable.
    """
    reglages = regles.tout().get("journal") or {}
    bilan_prive = journal.effacer_textes_ignores()
    if bilan_prive:
        log(f"{bilan_prive} transcription(s) d'énoncés ignorés effacée(s)")
    try:
        cible = journal.sauvegarder(os.path.expanduser("~/kinectface/etat/sauvegardes"), reglages.get("sauvegardes", 14))
        log(f"sauvegarde : {cible}")
    except Exception as e:
        log(f"ATTENTION : sauvegarde du journal impossible : {e}")
        journal.action("agent", "note", "sauvegarde du journal impossible", str(e), statut="a_verifier")
        return                                   # on ne purge JAMAIS sans filet
    try:
        bilan = journal.purger(reglages)
        if bilan_prive:
            bilan["transcriptions d'énoncés ignorés effacées"] = bilan_prive
        if bilan:
            log("purge : " + ", ".join(f"{n} {quoi}" for quoi, n in bilan.items()))
            journal.action("agent", "entretien", "journal purgé",
                           json.dumps(bilan, ensure_ascii=False), statut="fait")
    except Exception as e:
        log(f"purge impossible : {e}")


def rechauffer_bulle():
    """Après l'agent, recharger le modèle de Bulle sur la carte graphique (sinon la première réponse du matin est lente)."""
    run(["curl", "-s", "http://localhost:11434/api/generate", "-d",
         json.dumps({"model": os.environ.get("LLM_MODEL", "gpt-oss:20b-32k"), "prompt": "ok", "stream": False, "keep_alive": -1})], timeout=300)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=10, help="nombre maximal d'incidents par nuit")
    ap.add_argument("--fin", default="06:30", help="heure locale (Montréal) après laquelle on ne commence plus de chantier")
    ap.add_argument("--heures", type=float, default=24)
    ap.add_argument("--essai", action="store_true", help="n'analyse pas à nouveau ; reprend les incidents du jour")
    a = ap.parse_args()
    nuit = datetime.date.today().isoformat()
    os.makedirs(CHANTIERS, exist_ok=True)
    if not a.essai:
        entretien()
        log("analyse")
        run([VENV_PY, os.path.join(ICI, "analyste.py"), "--heures", str(a.heures)], cwd=DEPOT, timeout=900)
    exploitation(nuit)
    choisis = a_traiter(nuit, a.max)
    log(f"{len(choisis)} incident(s) à traiter")
    if choisis:
        log("banc de référence sur la production")
        reference = banc(DEPOT, "ws://127.0.0.1:8802/ws", os.path.join(CHANTIERS, f"reference-{nuit}.json"))
        log(f"référence : {reference['reussis']}/{reference['total']}")
        # Le modèle de l'agent (17,7 Go + contexte) ne tient à côté ni de la synthèse vocale ni de celui de Bulle.
        # Le décharger une fois ne suffit PAS : ia-warmup l'épingle (keep_alive -1) et sa minuterie repasse toutes
        # les 30 min — en plein milieu d'un chantier, qui dure jusqu'à 20 min. Dans la nuit du 21/09, Ollama n'a
        # chargé que 32 des 66 couches de l'agent sur la carte : la moitié du modèle tournait sur le processeur,
        # et 5 chantiers sur 6 ont expiré. On arrête donc la minuterie, pas seulement le modèle.
        run(["sudo", "systemctl", "stop", "ia-warmup.timer"])
        avant = _vram_libre()
        run(["sudo", "systemctl", "stop", "kyutai-tts"])
        run(["curl", "-s", "http://localhost:11434/api/generate", "-d",
             json.dumps({"model": os.environ.get("LLM_MODEL", "gpt-oss:20b-32k"), "keep_alive": 0})])
        time.sleep(6)
        libre = _vram_libre()
        gagne = libre - avant
        if gagne < 1500:
            # le 21/09, Kyutai est passé de systemd à des conteneurs Docker (Unmute) : l'arrêt ne libérait plus rien,
            # l'agent et Bulle se sont partagé une carte pleine et le modèle de Bulle ne pouvait plus se charger.
            log(f"ATTENTION : la libération de VRAM n'a rien donné ({gagne} Mio) — le service de synthèse a-t-il changé ?")
            journal.action("agent", "note", "libération de VRAM sans effet",
                           f"arrêt de kyutai-tts et du modèle de Bulle : {gagne} Mio récupérés, {libre} Mio libres", statut="a_verifier")
        # Mieux vaut une nuit blanche annoncée que trois heures de chantiers qui expirent l'un après l'autre :
        # sans la place pour le modèle, aucun ne peut aboutir, et on le sait AVANT de commencer.
        assez = libre >= VRAM_AGENT_MIO
        if not assez:
            log(f"ATTENTION : {libre} Mio libres pour un modèle qui en demande {VRAM_AGENT_MIO} — "
                f"Ollama le mettrait en partie sur le processeur et tous les chantiers expireraient. On n'entame rien.")
            journal.action("agent", "note", "nuit annulée : pas assez de VRAM pour l'agent",
                           f"{libre} Mio libres, {VRAM_AGENT_MIO} nécessaires ({gagne} Mio récupérés en arrêtant "
                           f"la synthèse et le modèle de Bulle). Qui occupe la carte ? « nvidia-smi ».",
                           statut="a_verifier")
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo("America/Toronto")
            h, m = map(int, a.fin.split(":"))
            for inc, detail in (choisis if assez else []):
                maintenant = datetime.datetime.now(tz)
                if (maintenant.hour, maintenant.minute) >= (h, m) and maintenant.hour < 12:
                    log(f"{a.fin} passé : on s'arrête (le reste attendra la nuit suivante)"); break
                try: chantier(inc, detail, nuit, reference)
                except Exception as e:
                    log(f"chantier {inc['cle']} en échec : {e}")
                    journal.action("agent", "note", f"chantier en échec : {inc['cle']}", str(e), statut="rejete_auto")
        finally:
            run(["curl", "-s", "http://localhost:11434/api/generate", "-d", json.dumps({"model": MODELE.split("/", 1)[1], "keep_alive": 0})])
            run(["sudo", "systemctl", "start", "kyutai-tts"])
            rechauffer_bulle()
            run(["sudo", "systemctl", "start", "ia-warmup.timer"])   # en dernier : qu'il ne reparte pas pendant le réchauffage
    log("fin de la nuit")


if __name__ == "__main__":
    main()
