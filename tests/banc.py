"""Banc de tests de Bulle : rejoue tests/banc.yaml en mode simulation contre un cerveau et note chaque cas.

  python tests/banc.py                                  cerveau de production (ws://localhost:8802/ws)
  python tests/banc.py --url ws://localhost:8803/ws     cerveau de test (branche de l'agent)
  python tests/banc.py --json resultat.json --repetitions 2
  python tests/banc.py --confirmer 2                    rejoue les ÉCHECS deux fois de plus (ce que fait la nuit)
Code de sortie : 0 si tout passe, 1 sinon. Le LLM n'est pas déterministe : --repetitions N rejoue chaque cas N fois
et un cas passe s'il réussit à la majorité des essais. --confirmer N ne rejoue que les cas où un échec est apparu :
c'est l'échec qui fait rejeter une correction, et c'est donc lui seul qui mérite d'être payé trois fois.

Le rapport JSON porte, par cas, le taux de réussite. `comparer()` s'en sert pour distinguer une vraie régression
d'un cas qui oscille — voir sa docstring. La boucle de nuit et le tech lead appellent tous deux cette fonction.
"""
import argparse, asyncio, json, os, sys, time, unicodedata

import websockets
import yaml

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ICI, "..", "cerveau"))
import regles  # noqa: E402  (liste des outils en lecture seule)


def norm(t):
    return "".join(c for c in unicodedata.normalize("NFD", str(t).lower()) if unicodedata.category(c) != "Mn")


def conforme(attendu, valeur):
    if isinstance(attendu, str) and attendu.startswith("~"):
        return norm(attendu[1:]) in norm(valeur)
    if isinstance(attendu, bool) or isinstance(valeur, bool):
        return bool(attendu) == bool(valeur) and valeur is not None
    try:
        return float(attendu) == float(valeur)
    except (TypeError, ValueError):
        return norm(attendu) == norm(valeur)


def juger(cas, appels, reponse, cartes=()):
    """→ (réussi, raison)."""
    noms = [a["name"] for a in appels]
    for i in cas.get("interdits", []):
        if i in noms: return False, f"outil interdit appelé : {i}"
    if cas.get("aucun_outil"):
        actions = [n for n, a in zip(noms, appels) if not lecture_seule(n, a.get("args") or {})]
        if actions: return False, f"action inattendue : {actions}"
    if "outil" in cas:
        voulus = cas["outil"] if isinstance(cas["outil"], list) else [cas["outil"]]
        candidats = [a for a in appels if a["name"] in voulus]
        if not candidats: return False, f"attendu {voulus}, appelé {noms or 'rien'}"
        for k, v in (cas.get("args") or {}).items():
            if not any(conforme(v, (a.get("args") or {}).get(k)) for a in candidats):
                return False, f"paramètre {k} : attendu {v}, reçu {[ (a.get('args') or {}).get(k) for a in candidats]}"
    if cas.get("reponse") and not conforme(cas["reponse"], reponse):
        return False, f"réponse sans « {cas['reponse'][1:]} » : {reponse[:80]}"
    if cas.get("reponse_sans") and conforme(cas["reponse_sans"], reponse):
        return False, f"réponse contient « {cas['reponse_sans'][1:]} » : {reponse[:80]}"
    if "carte" in cas:                       # « aucune » = la TV doit rester sur le visage seul
        gabarits = [(c or {}).get("gabarit") for c in cartes]
        if cas["carte"] == "aucune":
            if gabarits: return False, f"carte affichée alors qu'on n'en veut pas : {gabarits}"
        elif cas["carte"] not in gabarits:
            return False, f"carte {cas['carte']} attendue, affiché {gabarits or 'rien'}"
    return True, "ok"


# Au-dessus de ce taux, un cas passe FRANCHEMENT ; en dessous de son complément, il échoue franchement. Entre les
# deux, il oscille — et un cas qui oscille ne dit rien sur la correction qu'on est en train de juger.
FRANC = 0.8


def taux(cas):
    """Proportion d'essais réussis pour un cas d'un rapport de banc (rapports d'avant : un booléen)."""
    if "taux" in cas: return cas["taux"]
    essais = cas.get("essais") or []
    if essais: return sum(bool(e.get("ok")) for e in essais) / len(essais)
    return 1.0 if cas.get("reussi") else 0.0


def comparer(reference, apres, franc=FRANC):
    """Deux rapports de banc → (régressions franches, cas instables).

    Le LLM n'est pas déterministe : un cas qui échoue une fois sur trois échouera aussi, un jour, sans qu'on y
    ait touché. Le compter comme une régression fait rejeter une correction saine, que l'agent rejouera la nuit
    suivante — deux nuits perdues pour un tirage au sort. On ne retient donc comme régression que ce qui passait
    franchement AVANT et échoue franchement APRÈS ; le reste est signalé comme instable, ce qui est un défaut du
    cas de test lui-même, pas de la correction.
    """
    avant = {c["phrase"]: taux(c) for c in reference.get("cas") or []}
    regressions, instables = [], []
    for c in apres.get("cas") or []:
        ref = avant.get(c["phrase"])
        if ref is None: continue                      # cas ajouté par l'agent : jugé à part
        maintenant = taux(c)
        if ref >= franc and maintenant <= 1 - franc:
            regressions.append(c["phrase"])
        elif any(1 - franc < t < franc for t in (ref, maintenant)):
            instables.append(c["phrase"])
    return regressions, instables


def lecture_seule(nom, args):
    if regles.correspond(nom, regles.c("lecture_seule", [])): return True
    if nom == "twenty_execute_tool":
        return any(str(args.get("toolName", "")).startswith(p) for p in regles.c("twenty_lecture", []))
    return False


async def jouer(url, phrase, delai=120):
    async with websockets.connect(url, max_size=None, open_timeout=10,
                                  additional_headers={"Authorization": "Bearer " + regles.jeton()}) as ws:
        await ws.send(json.dumps({"type": "simulation", "actif": True}))
        await ws.send(json.dumps({"type": "text", "text": phrase}))
        appels, reponse, cartes, t0 = [], [], [], time.time()
        while time.time() - t0 < delai:
            m = await asyncio.wait_for(ws.recv(), timeout=delai)
            if not isinstance(m, str): continue
            d = json.loads(m)
            if d["type"] == "tool": appels.append(d)
            elif d["type"] == "sentence": reponse.append(d["text"])
            elif d["type"] == "carte" and d.get("carte"): cartes.append(d["carte"])
            elif d["type"] in ("done", "error"): break
        return appels, " ".join(reponse), cartes, round(time.time() - t0, 1)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8802/ws")
    ap.add_argument("--fichier", default=os.path.join(ICI, "banc.yaml"))
    ap.add_argument("--repetitions", type=int, default=1)
    ap.add_argument("--confirmer", type=int, default=0,
                    help="essais SUPPLÉMENTAIRES, joués seulement si un échec est apparu (confirme un échec sans "
                         "payer le triple sur toute la liste)")
    ap.add_argument("--filtre", help="ne jouer que les cas dont la phrase contient ce texte")
    ap.add_argument("--json", help="écrit le résultat détaillé dans ce fichier")
    a = ap.parse_args()
    cas = yaml.safe_load(open(a.fichier, encoding="utf-8"))["cas"]
    if a.filtre: cas = [c for c in cas if norm(a.filtre) in norm(c["phrase"])]
    res, ok_total = [], 0
    for c in cas:
        essais = []

        async def un_essai():
            try:
                appels, rep, cartes, duree = await jouer(a.url, c["phrase"])
                ok, raison = juger(c, appels, rep, cartes)
            except Exception as e:
                appels, rep, cartes, duree, ok, raison = [], "", [], 0, False, f"erreur : {e}"
            essais.append({"ok": ok, "raison": raison, "outils": [(x["name"], x.get("args")) for x in appels], "reponse": rep,
                           "cartes": [x.get("gabarit") for x in cartes], "duree": duree})

        for _ in range(a.repetitions):
            await un_essai()
        # Un échec vaut d'être confirmé : c'est lui qui fait rejeter une correction, et le LLM en produit au
        # hasard. Un succès, lui, ne coûte rien à croire. Rejouer seulement les échecs garde la nuit dans sa
        # fenêtre : la cinquantaine de cas reste à un essai, seuls les quelques ratés en paient trois.
        while a.confirmer and len(essais) < a.repetitions + a.confirmer and not all(e["ok"] for e in essais):
            await un_essai()
        t = sum(e["ok"] for e in essais) / len(essais)
        reussi = t * 2 > 1
        ok_total += reussi
        res.append({"phrase": c["phrase"], "reussi": reussi, "taux": t, "essais": essais, "origine": c.get("origine", "")})
        # « 2/3 » en dit plus que « OK » : c'est ce qui distingue un cas solide d'un cas qui joue à pile ou face
        compte = f"{sum(e['ok'] for e in essais)}/{len(essais)}" if len(essais) > 1 else ""
        print(f"{'OK ' if reussi else 'ÉCHEC'} {compte:>4}  {c['phrase'][:56]:<56} "
              f"{'' if reussi else essais[-1]['raison']}", flush=True)
    score = {"reussis": ok_total, "total": len(cas), "taux": round(ok_total / max(1, len(cas)), 3), "cas": res}
    print(f"\n{ok_total}/{len(cas)} cas réussis ({score['taux']:.0%})")
    if a.json:
        json.dump(score, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    sys.exit(0 if ok_total == len(cas) else 1)


if __name__ == "__main__":
    asyncio.run(main())
