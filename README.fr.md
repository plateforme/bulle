# BulleAI

Bulle est un assistant vocal **100 % local** qui vit sur la TV du salon : un visage minimaliste (noir et blanc + accent corail),
une Kinect 1 pour entendre et suivre du regard, et l'IA locale du VM .31 pour comprendre et agir (maison, musique, agenda, CRM…).

## Architecture

| Où | Quoi | Fichiers |
|---|---|---|
| **Pi 3 « JARVIS »** (192.0.2.6) | visage (shader GLES2, 30 ips), client audio (4 micros Kinect, Silero, écoute orientée), suivi Kinect (regard, présence) | `face.py`, `compagnon.py`, `pi/tracker.py` |
| | cartes affichées à côté du visage (mise en page locale) | `carte.py` |
| **VM .31** (RTX 3090) | cerveau : Whisper → LLM `gpt-oss:20b` + outils → étiquette d'émotion → Kyutai | `cerveau/server.py` |
| | cartes : résultat d'outil → données affichables | `cerveau/cartes.py` |
| | règles de comportement (rechargées à chaud) | `config/regles.yaml` |
| | outils composés (musique, mode club, lumières…) | `cerveau/outils_composes.py` |
| | journal SQLite (échanges, retours, incidents, actions, tests) + page de suivi `/suivi` | `cerveau/journal.py`, `cerveau/suivi.py` |

## Le choix du modele

`gpt-oss:20b-32k`, et ce n'est pas un classement qui le dit — c'est le banc. La contrainte n'est pas la qualite
des modeles, c'est la place : la 3090 est partagee, Whisper y tient 4,6 Go et la synthese le reste, il ne se
libere qu'une quinzaine de gigaoctets. `qwen3.8:27b` (17 Go) et `qwen3.5:35b-a3b-40k` (23 Go) ne rentrent pas a
cote de Bulle — c'est pour ca que l'agent de nuit ne tourne qu'a 3 h. Le seul rival qui rentre est
`gemma4:26b-a4b-it-qat` (14 Go).

Les deux, le 21/09/2026, memes conditions : meme code, meme selection d'outils, `--confirmer 2`, instance de
test 8813. Les temps viennent de `/metrics` de cette instance, pas d'un chronometre exterieur.

| | `gpt-oss:20b-32k` | `gemma4:26b-a4b-it-qat` |
|---|---|---|
| banc, au moment de la comparaison (51 cas) | **51/51** | 50/51 |
| GPU de reflexion par echange | **1,50 s** | 2,33 s |
| temps de modele jusqu'a la premiere phrase | **1,59 s** | 2,54 s |
| sur la carte | 12,9 Go | 14 Go |

Le banc a grossi depuis : gpt-oss passe **58/58** aujourd'hui.

Attention a la troisieme ligne : elle ne mesure que le modele. Elle demarre quand Whisper a fini et s'arrete
quand la phrase entre dans la file de la voix, donc elle ignore les deux bouts. Ce qu'on attend dans la piece
est plus long : environ une seconde pour que le Pi decide qu'on a fini de parler, 0,7 s de Whisper, puis le
modele, puis environ 2 s pour synthetiser la premiere phrase. Sur la video de demonstration, on compte 5 a 8 s
entre la fin de la question et le premier mot. Les deux sont mesures : `bulle_premiere_phrase` pour le modele
seul, `bulle_premier_son` pour le delai ressenti.

Pour le choix du modele, c'est bien la troisieme ligne qui tranche, puisque le reste de la chaine est le meme
des deux cotes. gpt-oss gagne partout, la question est reglee jusqu'au prochain modele qui tienne dans quinze
gigaoctets.

Deux choses a savoir quand meme : le format Harmony de gpt-oss echoue sur les appels d'outils **paralleles**,
donc chaque outil part dans son propre tour ; et `LLM_THINK=low` est deliberement bas — on paie un modele a
raisonnement sans en prendre le benefice, ce qui est le bon arbitrage pour la voix mais rend son classement
« intelligence » sans rapport avec ce qu'on lui demande.

Pour refaire la mesure : demarrer une instance de test avec `LLM_MODEL=<modele>`, jouer
`tests/banc.py --url ws://127.0.0.1:8813/ws --confirmer 2`, puis lire `/metrics` de cette instance
(`bulle_premiere_phrase_secondes_total` divise par `bulle_premieres_phrases_total`).

## Cartes (v1)

Ce que la voix dit mal — une liste, des chiffres, une orthographe — s'affiche **à côté** du visage : le visage se range
dans un tiers de l'écran, jette un coup d'œil à la carte, et la carte s'efface toute seule au bout de 15 s.

- La voix reste autosuffisante : depuis la cuisine, on ne rate rien. La carte ne fait que préciser.
- La carte est le rendu d'un **résultat d'outil** (`cerveau/cartes.py`), jamais du texte du LLM : pas de mise en page
  hallucinée, pas de latence ajoutée. Elle part pendant la réflexion, donc elle occupe les 3-6 s de silence.
- Pour ce qu'aucun outil ne produit (épeler un nom, un choix numéroté), le LLM appelle l'outil `afficher`.
- Le cerveau envoie des **données**, pas une image : le Pi pourra remettre en page selon la distance mesurée par la
  Kinect (v2) sans rien redemander au réseau.
- Gabarits : `liste`, `cles`, `media`, `texte` (`carte.py`). Contrôle de lisibilité sur la TV : touche **C** du visage.
- Réglages à chaud : `cerveau.cartes` dans `config/regles.yaml` (`actives`, `duree_s`, `exclues`).

## Amélioration continue

```
3 h (VM, minuterie bulle-nuit.timer)          7 h 30 (PC, tâche planifiée)     
analyste ─► incidents ─► agent local ─► branche agent/…  ─►  revue tech lead ─► production ─► bilan Signal « BulleAI »
 (journaux, SQLite, santé)  (OpenCode + qwen,     validée par le banc     (fusion testée,      + page /suivi
                             zone restreinte)     sur une instance de test  retour arrière auto)
```

- **Analyste** (`agent/analyste.py`, sans IA) : journaux du Pi et du cerveau + SQLite + bilans de santé → incidents classés,
  rapprochés de la base de pannes (`connaissances/pannes.yaml`), rapport dans `~/kinectface/etat/rapports/`.
- **Agent local** (`agent/boucle.py`, `agent/consigne_agent.md`) : OpenCode + `qwen3.8:27b`, dans une branche isolée,
  limité aux règles, aux outils composés, au banc de tests et à la base de pannes. Il ajoute toujours un cas de test.
- **Juge** : le banc de tests (`tests/banc.yaml`, `tests/banc.py`) rejoué en **mode simulation** (aucun effet sur la maison)
  sur une instance de test du cerveau (port 8813). Une régression = rejet.
- **Tech lead** (`agent/revue.py`) : revue, fusion avec banc en production et retour arrière automatique, ou refus motivé
  (relu par l'agent la nuit suivante). Bilan dans le groupe Signal « BulleAI » (`agent/signal_envoi.py`).
- **Ton retour** : dis « Bulle, ça ne marche pas » / « c'est pas ça » après une mauvaise réponse, ou coche ✗ sur `/suivi`.

## Déploiement

- Dépôt de travail : VM `~/kinectface/depot` (le cerveau tourne depuis là). Miroir : GitHub `plateforme/BulleAI`.
- Pi : `outils/deployer_pi.sh` (depuis le VM, ne redémarre que ce qui a changé).
- Diagnostic Kinect : `pi/ghost_photo.py` (photo couleur + profondeur, ce que le suivi prend pour une personne).
