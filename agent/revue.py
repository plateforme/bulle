"""Outils du tech lead : revue des branches de l'agent, mise en production avec retour arrière, refus motivé.

  python agent/revue.py liste                         branches en attente (verdict, rapport, diff, banc)
  python agent/revue.py fusionner <branche> [--note "…"]
  python agent/revue.py refuser <branche> "raison"
  python agent/revue.py bilan [--heures 26]            texte du bilan (pour Signal)
La fusion : merge --no-ff dans main → redémarrage du cerveau (+ déploiement Pi si besoin) → santé → banc en production ;
en cas de régression par rapport au banc d'avant, retour arrière automatique (revert) et redéploiement.
"""
import argparse, json, os, re, subprocess, sys, time

ICI = os.path.dirname(os.path.abspath(__file__))
DEPOT = os.path.dirname(ICI)
sys.path.insert(0, os.path.join(DEPOT, "cerveau"))
sys.path.insert(0, os.path.join(DEPOT, "tests"))
import journal  # noqa: E402
import banc as juge  # noqa: E402  (même règle « régression vs instable » que la boucle de nuit)

VENV_PY = os.path.expanduser("~/kinectface/venv/bin/python")
CHANTIERS = os.path.expanduser("~/kinectface/chantiers")
# C'est ici qu'une fausse régression coûte le plus cher : elle annule une fusion déjà en production. On rejoue
# donc les échecs, comme la boucle de nuit (banc.py --confirmer).
REPETITIONS = int(os.environ.get("BULLE_BANC_REPETITIONS", "1"))
CONFIRMER = int(os.environ.get("BULLE_BANC_CONFIRMER", "2"))


def run(cmd, timeout=1800):
    r = subprocess.run(cmd, cwd=DEPOT, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def en_attente():
    return journal.lire("select * from actions where type = 'proposition' and statut = 'en_attente' order by id")


def liste():
    props = en_attente()
    if not props:
        print("aucune branche en attente"); return
    for p in props:
        d = json.loads(p["detail"] or "{}")
        b = p["commit_ref"]
        meta = {}
        try: meta = json.load(open(os.path.join(CHANTIERS, b.replace("/", "_") + ".json"), encoding="utf-8"))
        except Exception: pass
        print("=" * 100)
        print(f"BRANCHE {b}  |  verdict orchestrateur : {d.get('verdict')}  |  banc : {meta.get('banc')}")
        print("raisons :", d.get("raisons"))
        print("\n--- RAPPORT DE L'AGENT ---\n" + (d.get("rapport") or "(aucun)"))
        print("\n--- DIFF (main...branche) ---")
        print(run(["git", "diff", f"main...{b}", "--", ".", ":!RAPPORT_AGENT.md"])[1][:12000])


def _banc_prod(nom):
    sortie = os.path.join(CHANTIERS, f"{nom}.json")
    run([VENV_PY, "tests/banc.py", "--json", sortie, "--repetitions", str(REPETITIONS), "--confirmer", str(CONFIRMER)])
    try: return json.load(open(sortie, encoding="utf-8"))
    except Exception: return {"reussis": 0, "total": 0, "cas": []}


def _redemarrer(fichiers):
    run(["sudo", "systemctl", "restart", "kinectface-cerveau"])
    for _ in range(30):
        time.sleep(1)
        if run(["curl", "-sf", "http://127.0.0.1:8802/health"], timeout=30)[0] == 0: break
    else:
        return False, "le cerveau ne redémarre pas"
    if any(f in ("config/regles.yaml", "face.py", "carte.py", "compagnon.py") or f.startswith("pi/") for f in fichiers):
        rc, out = run(["bash", "outils/deployer_pi.sh"])
        if rc: return False, "déploiement Pi : " + out[-300:]
    return True, "ok"


def _controles():
    """Validation des YAML + tests unitaires, comme la CI → message d'échec, ou None si tout passe."""
    for titre, cmd in (("fichiers YAML", [VENV_PY, "outils/valider.py"]),
                       ("tests unitaires", [VENV_PY, "-m", "pytest"])):
        rc, out = run(cmd, timeout=600)
        if rc:
            return f"{titre} : {out.strip()[-400:]}"
    return None


def fusionner(b, note=""):
    prop = next((p for p in en_attente() if p["commit_ref"] == b), None)
    if not prop: sys.exit(f"{b} n'est pas en attente")
    d = json.loads(prop["detail"] or "{}")
    avant = _banc_prod(f"avant-{int(time.time())}")
    fichiers = run(["git", "diff", "--name-only", f"main...{b}"])[1].split()
    rc, out = run(["git", "merge", "--no-ff", "-m", f"Revue tech lead : fusion de {b}\n\n{note}".strip(), b])
    if rc: sys.exit("fusion impossible : " + out)
    # Les contrôles sont rejoués ICI, sur main fusionné : la boucle de nuit les a passés sur la branche seule,
    # mais c'est l'état fusionné qui part en production, et deux chantiers d'une même nuit peuvent se contredire.
    # Ils durent deux secondes et évitent de redémarrer le cerveau sur du code qui ne tient pas debout.
    echec = _controles()
    if echec:
        run(["git", "reset", "--hard", "HEAD~1"])
        journal.action("tech_lead", "revue", f"fusion annulée : {b}",
                       json.dumps({"cle": d.get("cle"), "raison": echec}, ensure_ascii=False),
                       commit_ref=b, statut="refuse")
        journal.ecrire("update actions set statut = 'refuse' where id = ?", (prop["id"],))
        print("FUSION ANNULÉE (contrôles) :", echec); sys.exit(1)
    ok, msg = _redemarrer(fichiers)
    apres = _banc_prod(f"apres-{int(time.time())}") if ok else {"cas": []}
    regressions, instables = juge.comparer(avant, apres)
    if instables:
        # un cas qui oscille n'est pas une raison d'annuler une fusion : on le dit, on ne revient pas en arrière
        print("cas instables (le verdict les ignore) :", instables)
    if not ok or regressions:
        run(["git", "revert", "-m", "1", "--no-edit", "HEAD"])
        _redemarrer(fichiers)
        raison = msg if not ok else f"régressions en production : {regressions}"
        journal.action("tech_lead", "revue", f"fusion annulée : {b}", json.dumps({"cle": d.get("cle"), "raison": raison}, ensure_ascii=False),
                       commit_ref=b, statut="refuse")
        journal.ecrire("update actions set statut = 'refuse' where id = ?", (prop["id"],))
        print("RETOUR ARRIÈRE :", raison); sys.exit(1)
    commit = run(["git", "rev-parse", "--short", "HEAD"])[1].strip()
    journal.ecrire("update actions set statut = 'applique' where id = ?", (prop["id"],))
    journal.action("tech_lead", "revue", f"mis en production : {prop['titre']}", json.dumps({"cle": d.get("cle"), "note": note}, ensure_ascii=False),
                   commit_ref=commit, statut="applique")
    journal.ecrire("update incidents set statut = 'corrige' where cle = ? and statut in ('propose','ouvert')", (d.get("cle"),))
    m = re.search(r"## À tester par Greg\s*\n(.+?)(\n## |\Z)", d.get("rapport") or "", re.S)
    if m and m.group(1).strip().lower().strip(" .«»\"") not in ("rien", "aucun", ""):
        journal.a_tester("agent " + b, m.group(1).strip()[:300], prop["titre"])
    run(["git", "branch", "-D", b])
    print(f"EN PRODUCTION : {b} ({commit}) — banc {apres.get('reussis')}/{apres.get('total')}")


def refuser(b, raison):
    prop = next((p for p in en_attente() if p["commit_ref"] == b), None)
    if not prop: sys.exit(f"{b} n'est pas en attente")
    d = json.loads(prop["detail"] or "{}")
    journal.ecrire("update actions set statut = 'refuse' where id = ?", (prop["id"],))
    journal.action("tech_lead", "revue", f"refusé : {prop['titre']}", json.dumps({"cle": d.get("cle"), "raison": raison}, ensure_ascii=False),
                   commit_ref=b, statut="refuse")
    journal.ecrire("update incidents set statut = 'ouvert' where cle = ? and statut = 'propose'", (d.get("cle"),))
    run(["git", "branch", "-D", b])
    print(f"REFUSÉ : {b} — {raison}")


def _court(t, n=110):
    """Une ligne lisible sur téléphone : la première phrase, coupée proprement (le détail est sur la page de suivi)."""
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0].rstrip(" ,;:(«") + "…"


def bilan(heures):
    depuis = time.time() - heures * 3600
    acts = journal.lire("select * from actions where ts >= ? order by id", (depuis,))
    faits = [a for a in acts if (a["type"] == "revue" and a["statut"] == "applique") or a["type"] == "correction"]
    refus = [a for a in acts if a["type"] == "revue" and a["statut"] == "refuse"]
    expl = [a for a in acts if a["type"] == "exploitation"]
    diag = [a for a in acts if a["type"] == "proposition" and '"diagnostic_seul"' in (a["detail"] or "")]
    # un cas qui réussit une fois sur trois ne juge plus rien : c'est le cas de test qui est à revoir, pas Bulle
    instables = [a for a in acts if a["type"] == "note" and a["statut"] == "instable"]
    tests = journal.lire("select * from a_tester where statut = 'a_faire' order by id")
    inc = journal.lire("select type, count(*) n from incidents where ts >= ? group by type order by n desc", (depuis,))
    st = journal.lire("select count(*) n, sum(ignore_raison is null) r from echanges where ts >= ? and simulation = 0", (depuis,))[0]
    l = [f"🫧 Bulle — bilan du {time.strftime('%d/%m')}", f"{st['r'] or 0} réponses, {len(inc)} types d'incidents repérés."]
    if faits: l += ["", "✅ Mis en production :"] + [f"• {a['titre'].replace('mis en production : ', '')}"
                                                        + (" (tech lead)" if a["type"] == "correction" else "") for a in faits]
    if expl: l += ["", "🔧 Corrigé en exploitation :"] + [f"• {a['titre']}" for a in expl]
    if refus: l += ["", "❌ Refusé (l'agent réessaiera) :"] + [f"• {a['titre'].replace('refusé : ', '')} — {_court(json.loads(a['detail'] or '{}').get('raison', ''))}" for a in refus]
    if instables: l += ["", "🎲 Cas de test instables (à revoir, ce n'est pas Bulle) :"] + [
        f"• {a['titre'].replace('cas de test instable : ', '')}" for a in instables]
    if tests: l += ["", "🎙️ À tester (dis à Bulle) :"] + [f"• {t['phrase']}" for t in tests[:6]]
    if diag: l += ["", "🙋 Pour toi :"] + [f"• {a['titre']}" for a in diag]
    if not (faits or expl or refus or tests or diag or instables): l += ["", "Rien à signaler cette nuit."]
    l += ["", "Détails : http://192.0.2.31:8802/suivi"]
    print("\n".join(l))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("liste")
    f = sp.add_parser("fusionner"); f.add_argument("branche"); f.add_argument("--note", default="")
    r = sp.add_parser("refuser"); r.add_argument("branche"); r.add_argument("raison")
    b = sp.add_parser("bilan"); b.add_argument("--heures", type=float, default=26)
    a = ap.parse_args()
    {"liste": liste, "fusionner": lambda: fusionner(a.branche, a.note), "refuser": lambda: refuser(a.branche, a.raison),
     "bilan": lambda: bilan(a.heures)}[a.cmd]()


if __name__ == "__main__":
    main()
