"""Génère le tableau de bord Grafana « Bulle — l'assistant du salon » (dossier « IA locale »).

À quoi il répond : Bulle partage la RTX 3090 avec Open WebUI, l'IDE d'un collègue et l'agent de nuit. Quand le GPU
rame, la question qui vient est « est-ce l'assistant du salon ? ». Ce tableau de bord donne le chiffre plutôt
qu'une impression, et il le donne **au regard des autres usagers** — d'où les panneaux qui mêlent ses mesures à
elle (`bulle_*`, servies par le cerveau sur /metrics) et celles du collecteur commun (`ia_*`, servies par
ia-usage sur la VM).

Un échange coûte trois choses, et une seule passait par le relais nginx que le collecteur sait lire :
  transcription (Whisper)  →  réflexion (Ollama, vue par le relais)  →  synthèse (Kyutai)
Les deux autres n'étaient comptées nulle part avant le 21/09/2026 ; c'est ce que `cerveau/mesures.py` a ajouté.

Le fichier vit ici parce qu'il décrit Bulle, mais il s'exécute sur CT101, où sont Prometheus et Grafana :

    scp outils/tableau_bulle.py …                       # → /root/bulle-dashboard.py sur CT101
    python3 /root/bulle-dashboard.py /opt/monitoring/provisioning/dashboards/ia-locale/bulle.json

Grafana relit le dossier tout seul en une dizaine de secondes (provisionné par fichier : l'API exige un jeton
que personne n'a). Voir aussi /root/ia-dashboard.py sur CT101, qui fait « IA locale — GPU et usage ».
"""
import json, sys

DS = {"type": "prometheus", "uid": "ia-prometheus"}
TARIF = 0.0819   # $/kWh, tarif résidentiel D Hydro-Québec (2e tranche, estimation) — même valeur qu'ia-dashboard.py
SALON = '{mode="salon"}'
panels = []
pid = [0]


def nid():
    pid[0] += 1; return pid[0]


def row(title, y):
    panels.append({"type": "row", "title": title, "id": nid(), "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []})


def tgt(expr, legend="", instant=False, fmt=None):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": "A"}
    if instant: t["instant"] = True; t["range"] = False
    if fmt: t["format"] = fmt
    return t


def targets(*ts):
    out = []
    for i, t in enumerate(ts):
        t = dict(t); t["refId"] = chr(65 + i); out.append(t)
    return out


def texte(md, x, y, w, h):
    panels.append({"type": "text", "title": "", "id": nid(), "gridPos": {"h": h, "w": w, "x": x, "y": y},
                   "options": {"mode": "markdown", "content": md}, "transparent": True})


def stat(title, x, y, w, h, ts, unit="none", thresholds=None, decimals=None, mappings=None, desc="",
         color_mode="value", graph="none", text_mode="auto"):
    fc = {"unit": unit, "color": {"mode": "thresholds"},
          "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "blue", "value": None}]}}
    if decimals is not None: fc["decimals"] = decimals
    if mappings: fc["mappings"] = mappings
    panels.append({"type": "stat", "title": title, "description": desc, "id": nid(), "datasource": DS,
                   "gridPos": {"h": h, "w": w, "x": x, "y": y}, "targets": targets(*ts),
                   "fieldConfig": {"defaults": fc, "overrides": []},
                   "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                               "colorMode": color_mode, "graphMode": graph, "justifyMode": "center",
                               "textMode": text_mode, "orientation": "auto"}})


def gauge(title, x, y, w, h, ts, unit="percent", mx=100, thresholds=None, desc="", decimals=None):
    fc = {"unit": unit, "min": 0, "max": mx, "color": {"mode": "thresholds"},
          "thresholds": {"mode": "absolute", "steps": thresholds}}
    if decimals is not None: fc["decimals"] = decimals
    panels.append({"type": "gauge", "title": title, "description": desc, "id": nid(), "datasource": DS,
                   "gridPos": {"h": h, "w": w, "x": x, "y": y}, "targets": targets(*ts),
                   "fieldConfig": {"defaults": fc, "overrides": []},
                   "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                               "showThresholdMarkers": True}})


def ts_panel(title, x, y, w, h, ts, unit="none", stack=False, fill=10, desc="", mn=None, mx=None, overrides=None,
             legend_calcs=None, style="line", largeur=2):
    d = {"unit": unit, "color": {"mode": "palette-classic"},
         "custom": {"drawStyle": style, "lineWidth": largeur, "fillOpacity": fill, "showPoints": "never",
                    "spanNulls": True, "stacking": {"mode": "normal" if stack else "none", "group": "A"}}}
    if mn is not None: d["min"] = mn
    if mx is not None: d["max"] = mx
    panels.append({"type": "timeseries", "title": title, "description": desc, "id": nid(), "datasource": DS,
                   "gridPos": {"h": h, "w": w, "x": x, "y": y}, "targets": targets(*ts),
                   "fieldConfig": {"defaults": d, "overrides": overrides or []},
                   "options": {"legend": {"displayMode": "table" if legend_calcs else "list", "placement": "bottom",
                                          "calcs": legend_calcs or []},
                               "tooltip": {"mode": "multi", "sort": "desc"}}})


def bargauge(title, x, y, w, h, ts, unit="m", desc="", decimals=1, overrides=None):
    panels.append({"type": "bargauge", "title": title, "description": desc, "id": nid(), "datasource": DS,
                   "gridPos": {"h": h, "w": w, "x": x, "y": y}, "targets": targets(*ts),
                   "fieldConfig": {"defaults": {"unit": unit, "min": 0, "color": {"mode": "palette-classic"},
                                                "decimals": decimals}, "overrides": overrides or []},
                   "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                               "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                               "valueMode": "color", "namePlacement": "left"}})


# Bulle en violet partout où elle côtoie les autres usagers : c'est la seule chose qu'on cherche des yeux.
VIOLET = [{"matcher": {"id": "byName", "options": "Bulle"},
           "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "purple"}}]}]
PART_BULLE = ('sum(increase(ia_temps_gpu_attribue_secondes_total{utilisateur="Bulle"}[$__range])) / '
              'sum(increase(ia_temps_gpu_attribue_secondes_total[$__range]))')
ENERGIE_BULLE = ('scalar(avg_over_time(ia_gpu_puissance_watts[$__range])) * '
                 'sum(increase(ia_temps_gpu_attribue_secondes_total{utilisateur="Bulle"}[$__range])) / 3600000')

y = 0
texte("""### Bulle, l'assistant vocal du salon — ce qu'elle prend au serveur d'IA

Bulle n'a pas de carte graphique à elle : elle emprunte la **RTX 3090 partagée** avec Open WebUI, l'IDE d'un collègue et
l'agent de nuit. Un échange lui coûte trois étapes — **transcription** (Whisper), **réflexion** (Ollama),
**synthèse** (Kyutai) — et elle immobilise en plus de la mémoire vidéo en permanence, même quand personne ne lui
parle. Les panneaux ci-dessous disent combien, et au regard de qui.

*Rien de ce qui se dit au salon n'arrive jusqu'ici : des compteurs, des durées, des raisons — jamais un mot.*""",
      0, y, 24, 5)
y += 5

row("Bulle en ce moment", y); y += 1
stat("Le cerveau", 0, y, 4, 4, [tgt('up{job="bulle"}', instant=True)], color_mode="background",
     mappings=[{"type": "value", "options": {"0": {"text": "Injoignable", "color": "red"},
                                             "1": {"text": "En ligne", "color": "green"}}},
               {"type": "special", "options": {"match": "null", "result": {"text": "Jamais vu", "color": "text"}}}],
     desc="Le service kinectface-cerveau sur la VM .31. « Injoignable » = Bulle ne répond plus au salon.")
stat("Le salon", 4, y, 4, 4, [tgt("bulle_clients_connectes", instant=True)], color_mode="background",
     mappings=[{"type": "value", "options": {"0": {"text": "Personne au micro", "color": "text"},
                                             "1": {"text": "Le Pi écoute", "color": "green"}}},
               {"type": "range", "options": {"from": 2, "to": 99, "result": {"text": "Plusieurs clients", "color": "orange"}}}],
     desc="Connexions ouvertes sur /ws. Normalement une seule : le Pi « JARVIS » de la TV.")
stat("Modèle qui lui répond", 8, y, 5, 4, [tgt("bulle_modele_info", "{{modele}}", instant=True)], text_mode="name")
stat("Échanges (période affichée)", 13, y, 4, 4,
     [tgt('sum(increase(bulle_echanges_total{source!="banc"}[$__range]))', instant=True)], decimals=0,
     desc="Ce que Greg lui a demandé. Le banc de tests de nuit est exclu — il a son propre chiffre plus bas.")
stat("Délai avant qu'elle parle", 17, y, 4, 4,
     [tgt('sum(increase(bulle_premiere_phrase_secondes_total{source!="banc"}[$__range])) / '
          'sum(increase(bulle_premieres_phrases_total{source!="banc"}[$__range]))', instant=True)],
     unit="s", decimals=1,
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 3}, {"color": "red", "value": 6}],
     desc="Temps entre la fin de la question et la première phrase dite. C'est ce qu'on ressent comme la vitesse de "
          "Bulle : la suite se dit pendant que le modèle écrit encore.")
stat("Réponse complète", 21, y, 3, 4,
     [tgt('sum(increase(bulle_reponse_secondes_total{source!="banc"}[$__range])) / '
          'sum(increase(bulle_echanges_total{source!="banc"}[$__range]))', instant=True)],
     unit="s", decimals=1,
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 12}, {"color": "red", "value": 25}],
     desc="De la question à la dernière phrase. Dépend surtout du nombre d'outils appelés (météo, Spotify, lumières…).")
y += 4

row("Ce qu'elle prend au GPU, au regard des autres", y); y += 1
gauge("Part de Bulle dans le temps GPU", 0, y, 6, 7, [tgt("100 * " + PART_BULLE, instant=True)], decimals=1,
      thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 40}, {"color": "red", "value": 70}],
      desc="Part du temps de calcul que le modèle a passé pour Bulle, sur tout ce qu'il a fait pendant la période — "
           "sa réflexion seulement, c'est la seule étape que le relais nginx voit passer. Le collecteur répartit au "
           "prorata le temps que plusieurs usagers passent à s'attendre dans la même file : les parts s'additionnent "
           "donc exactement à 100 %.")
bargauge("Temps de calcul par usager (période affichée)", 6, y, 10, 7,
         [tgt('sum by (utilisateur) (increase(ia_temps_gpu_attribue_secondes_total{utilisateur!="système"}[$__range])) / 60',
              "{{utilisateur}}", instant=True)], overrides=VIOLET,
         desc="Les mêmes minutes que dans « IA locale — GPU et usage », Bulle comprise. Avant le 21/09/2026 elle n'y "
              "apparaissait pas : faute d'agent à elle dans le journal du relais, le collecteur la rangeait dans "
              "« système », un bac que le tableau de bord exclut.")
bargauge("Minutes de GPU pour Bulle, par étape", 16, y, 8, 7,
         [tgt("sum by (etape) (increase(bulle_etape_secondes_total%s[$__range])) / 60" % SALON, "{{etape}}", instant=True)],
         desc="Mesuré par le cerveau lui-même, aux trois bornes d'un échange. Seule la réflexion passe par le relais : "
              "la transcription et la synthèse sont du temps GPU que le tableau de bord commun ne voit pas, et qui "
              "manque donc dans la part ci-contre.")
y += 7

ts_panel("Occupation du modèle par usager", 0, y, 14, 8,
         [tgt("sum by (utilisateur) (rate(ia_temps_gpu_attribue_secondes_total[5m]))", "{{utilisateur}}")],
         unit="percentunit", stack=True, fill=35, mn=0, overrides=VIOLET, legend_calcs=["mean", "max"],
         desc="Part du temps pendant laquelle le modèle travaille pour chacun (moyenne glissante sur 5 minutes). "
              "100 % = il ne fait que ça. Les pointes de Bulle sont courtes : une question, une réponse, puis plus rien.")
stat("Mémoire vidéo immobilisée pour sa voix et son oreille", 14, y, 5, 4,
     [tgt('sum(ia_service_vram_octets{service=~"Kyutai.*|Speaches.*"})', instant=True)], unit="bytes", decimals=1,
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 5e9}, {"color": "red", "value": 8e9}],
     desc="Kyutai (synthèse) et Speaches/Whisper (transcription) gardent leur modèle en mémoire en permanence, même la "
          "nuit quand personne ne parle. C'est autant de mémoire que les autres n'ont pas pour charger un modèle : le "
          "coût de Bulle qui ne se voit pas dans les minutes. Kyutai sert aussi la voix d'Open WebUI.")
stat("… soit, de la carte", 19, y, 5, 4,
     [tgt('100 * sum(ia_service_vram_octets{service=~"Kyutai.*|Speaches.*"}) / scalar(ia_gpu_vram_totale_octets)',
          instant=True)],
     unit="percent", decimals=1,
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 25}, {"color": "red", "value": 40}],
     desc="Sur les 24 Go de la RTX 3090.")
stat("Énergie attribuable à Bulle", 14, y + 4, 5, 4, [tgt(ENERGIE_BULLE, instant=True)], unit="kwatth", decimals=3,
     desc="Estimation : puissance moyenne du GPU sur la période × les secondes qu'il a passées pour Bulle. Grossier — "
          "le GPU consomme moins au repos que sous charge — et il ne compte ni le reste du serveur, ni le Pi du salon.")
stat("… en électricité", 19, y + 4, 5, 4, [tgt("%s * %s" % (ENERGIE_BULLE, TARIF), instant=True)],
     unit="currencyUSD", decimals=3, desc="Au tarif D d'Hydro-Québec (%s $/kWh, estimation)." % TARIF)
y += 8

row("Un échange, étape par étape", y); y += 1
ts_panel("Part du temps où Bulle occupe un service", 0, y, 12, 8,
         [tgt("sum by (etape) (rate(bulle_etape_secondes_total%s[5m]))" % SALON, "{{etape}}")],
         unit="percentunit", stack=True, fill=35, mn=0, legend_calcs=["mean", "max"],
         desc="Empilé, parce que les trois étapes s'enchaînent sans se chevaucher à l'intérieur d'un échange. Une pile "
              "haute signifie que Bulle a parlé sans arrêt ; au-delà de 100 %, c'est que plusieurs échanges se sont "
              "chevauchés.")
ts_panel("Durée moyenne de chaque étape", 12, y, 12, 8,
         [tgt("sum by (etape) (increase(bulle_etape_secondes_total%s[15m])) / "
              "sum by (etape) (increase(bulle_etape_appels_total%s[15m]))" % (SALON, SALON), "{{etape}}")],
         unit="s", mn=0, legend_calcs=["mean", "max"],
         desc="La synthèse est mesurée par phrase et non par réponse : il est normal qu'elle soit courte. La réflexion "
              "est mesurée par tour de modèle — un échange qui appelle trois outils en compte quatre.")
y += 8

row("Ce qu'elle entend, ce qu'elle fait", y); y += 1
ts_panel("Échanges", 0, y, 8, 7, [tgt("sum by (source) (increase(bulle_echanges_total[$__interval]))", "{{source}}")],
         style="bars", fill=70, largeur=1, stack=True, mn=0,
         desc="Par origine : « voix » = parlé au salon, « texte » = tapé, « banc » = les cas rejoués la nuit par "
              "l'agent, qui consomment du vrai temps GPU mais que personne n'a demandés.")
ts_panel("Énoncés écartés", 8, y, 8, 7,
         [tgt("sum by (raison) (increase(bulle_enonces_ignores_total[$__interval]))", "{{raison}}")],
         style="bars", fill=70, largeur=1, stack=True, mn=0,
         desc="Le micro entend toute la pièce. « pas_nomme » = la phrase ne s'adressait pas à Bulle, « hallucination » = "
              "Whisper a inventé du texte sur du silence, « silence » = rien à transcrire. Il n'en reste que ce "
              "compteur : le texte d'un énoncé écarté n'est écrit nulle part, ni ici, ni en base, ni dans le journal "
              "du système.")
bargauge("Outils les plus appelés (période affichée)", 16, y, 8, 7,
         [tgt("topk(10, sum by (outil) (increase(bulle_outils_total[$__range])))", "{{outil}}", instant=True)],
         unit="short", decimals=0,
         desc="Ce que Bulle est allée chercher : météo, Spotify, Home Assistant, Jira… Chaque appel part sur le réseau "
              "et ne coûte rien au GPU, mais rallonge la réponse d'autant.")
y += 7

stat("Outils en échec", 0, y, 6, 4,
     [tgt('100 * sum(increase(bulle_outils_total{issue="erreur"}[$__range])) / '
          'sum(increase(bulle_outils_total[$__range]))', instant=True)],
     unit="percent", decimals=1,
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 5}, {"color": "red", "value": 15}],
     desc="Un outil en échec ne rend pas Bulle muette : elle répond quand même, souvent à côté. C'est le premier "
          "endroit à regarder quand elle raconte n'importe quoi.")
stat("Étapes en échec", 6, y, 6, 4, [tgt("sum(increase(bulle_etape_erreurs_total[$__range]))", instant=True)],
     decimals=0, thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
     desc="Une synthèse en échec, elle, rend Bulle muette : elle écrit alors sa réponse sur une carte, faute de "
          "pouvoir la dire.")
stat("Caractères dits", 12, y, 6, 4, [tgt("sum(increase(bulle_caracteres_synthetises_total[$__range]))", instant=True)],
     unit="short", decimals=0,
     desc="Envoyés à la synthèse vocale. Une réponse parlée tient normalement en deux ou trois phrases.")
stat("Échanges du banc de nuit", 18, y, 6, 4,
     [tgt('sum(increase(bulle_echanges_total{source="banc"}[$__range]))', instant=True)], decimals=0,
     desc="Les cas de tests rejoués par l'agent de nuit contre le cerveau de production. Ils consomment du temps GPU "
          "réel — d'où le canal « Banc de tests » distinct dans le classement par usager.")

dash = {"uid": "bulle", "title": "Bulle — l'assistant du salon", "tags": ["ia-locale", "bulle"], "timezone": "browser",
        "schemaVersion": 39, "version": 1, "refresh": "30s", "time": {"from": "now-24h", "to": "now"},
        "description": "Ce que l'assistant vocal du salon demande au serveur d'IA partagé : minutes de GPU au regard "
                       "des autres usagers, mémoire vidéo immobilisée en permanence par sa voix et son oreille, et le "
                       "détail d'un échange (transcription, réflexion, synthèse). Rien du contenu des conversations "
                       "n'y figure.",
        "links": [{"type": "link", "title": "IA locale — GPU et usage", "url": "/d/ia-locale-gpu",
                   "icon": "external link", "tooltip": "Le GPU dans son ensemble, tous usagers confondus",
                   "targetBlank": False, "tags": [], "asDropdown": False, "includeVars": True, "keepTime": True}],
        "panels": panels, "editable": True, "graphTooltip": 1, "annotations": {"list": []}, "templating": {"list": []}}
json.dump(dash, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("panneaux :", len(panels))
