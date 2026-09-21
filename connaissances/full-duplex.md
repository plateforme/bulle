# Couper la parole à Bulle : ce que le full-duplex demanderait vraiment

Note de conception, 21/09/2026. C'est la réponse à « est-ce qu'on peut parler à Bulle pendant qu'elle
parle », posée après la mesure du modèle et la sélection d'outils.

**État au 21/09/2026 au soir** : l'étape 0 (mesurer) et l'étape 2 (le coupe-parole) sont écrites et déployées ;
le coupe-parole est **désactivé** en attendant que la mesure ait parlé. Les étapes 1 (annulation d'écho) et 3
(anticipation de fin de tour) ne sont pas faites.

## Où va le temps aujourd'hui

La chaîne est **half-duplex par construction**, et une seule ligne le décide, dans `compagnon.py` :

```python
muted = lambda: "voix" if player.busy() else ("attente" if state["waiting"] else "")
```

Tant que Bulle parle, la capture est coupée. C'est là pour une bonne raison : sans annulation d'écho, Bulle
s'entend elle-même et se répond. Tout le reste découle de cette ligne — elle rend aujourd'hui *pourquoi* le
micro est fermé, parce que c'est seulement pendant « voix » qu'on mesure la fuite et qu'on peut se faire
couper la parole.

Le budget mesuré le 21/09 (`/metrics`, instance de test, gpt-oss) :

| | |
|---|---|
| silence exigé avant d'envoyer l'énoncé (`END_SILENCE`) | **0,75 s**, payées à chaque phrase |
| transcription (Whisper) | variable, mesurée par `bulle_etape_secondes_total{etape="transcription"}` |
| réflexion jusqu'à la première phrase | **1,59 s** |
| micro rouvert | seulement à la fin de la dernière phrase dite |

La cible que la littérature donne pour une conversation qui ne se sent pas robotique est **sous 500 ms** entre
la fin de la phrase de Greg et le début de la réponse. On en est loin, et les 750 ms de `END_SILENCE` sont la
part la plus bête : c'est du silence qu'on attend pour être sûr que la phrase est finie.

## L'ordre dans lequel ça se fait

**1. L'annulation d'écho, pour aller plus loin que le coupe-parole.** Le coupe-parole (étape 2) s'en passe :
il ne lit qu'un niveau, il ne transcrit rien. Mais tout ce qui demande de *comprendre* ce qui est dit pendant
que Bulle parle — répondre à une correction en cours de phrase, enchaîner sans attendre — a besoin d'un micro
réellement ouvert, donc d'un écho annulé. Et c'est là que se trouve le vrai coût : il faudrait la faire sur le
Pi 3, qui est déjà
bridé thermiquement une partie de la journée (fiche `pi-jarvis-bride-thermiquement`, `pi/tracker.py` publie le
pic des 24 h). Un AEC WebRTC ou Speex sur quatre canaux, en continu, sur ce processeur-là, est à mesurer avant
d'être promis. La sortie est connue — c'est nous qui l'envoyons — donc le signal de référence est gratuit ;
c'est le seul point facile.

**2. Le coupe-parole, qui est 80 % du bénéfice pour 20 % du travail — écrit, désactivé.** Pas besoin de
vrai full-duplex pour que « Bulle, tais-toi » marche : on arrête le lecteur et on vide la file dès qu'un niveau
dépasse franchement la fuite mesurée. C'est fait (`Player.couper`, `client.coupe_parole` dans `regles.yaml`),
et **`actif: false`** tant que la jauge n'a pas donné le pic réel — l'activer avant reviendrait à laisser Bulle
se couper la parole à elle-même.

Une contre-intuition à garder : **Silero ne sert à rien ici**. La synthèse de Bulle *est* une voix, le
détecteur la reconnaîtrait comme telle. Seul le niveau sépare quelqu'un qui parle de ce que le haut-parleur
renvoie — et c'est aussi ce qui coûte le moins cher au Pi.

Pour l'activer : lire `bulle_fuite_haut_parleur` sur quelques jours, vérifier qu'une vraie voix dépasse
confortablement `pic × marge`, puis passer `actif: true`. Le réglage est relu au démarrage du client.

Une limite assumée de cette version : **le cerveau, lui, finit sa réponse.** Le client jette les phrases qui
continuent d'arriver, mais l'échange est journalisé comme s'il avait été dit en entier et reste dans
l'historique de conversation — Bulle croit avoir dit ce qu'on ne l'a pas laissée finir. Le réparer demande de
pouvoir arrêter le cerveau, pas seulement le son.

**3. Anticiper la fin du tour.** Remplacer les 750 ms de `END_SILENCE` par une prédiction de fin de tour :
partir vers Whisper dès que la phrase *semble* finie, quitte à annuler. La littérature 2026 appelle ça
*endpoint anticipation*, et c'est la seule des quatre étapes dont le gain se lit directement dans une mesure
qu'on a déjà (`bulle_premiere_phrase_secondes_total` divisé par `bulle_premieres_phrases_total`). À faire
seulement après avoir ajouté une mesure du temps passé à attendre le silence : aujourd'hui il est invisible,
noyé dans ce que Greg ressent.

**4. Un modèle parole-à-parole natif — pas maintenant.** Moshi, BayLing-Duplex et les autres décident
d'écouter, de parler ou de se taire par simple prédiction de token, et Kyutai fait déjà notre synthèse, donc la
tentation est réelle. Mais un modèle parole-à-parole remplace Whisper **et** le LLM **et** la synthèse — donc
il emporte avec lui les cinquante-cinq outils, les cartes, Home Assistant, le CRM. C'est-à-dire tout ce qui
fait Bulle. Les modèles de ce genre n'appellent pas encore des outils de façon fiable, et gpt-oss vient de
faire 51/51 au banc. Tant qu'un modèle parole-à-parole ne passe pas le banc, ce n'est pas une évolution de
Bulle, c'est un autre produit.

## Ce qu'il faudrait mesurer avant de commencer

- **le pic de fuite du haut-parleur** — fait : `bulle_fuite_haut_parleur`, remonté par le Pi à chaque
  énoncé. C'est ce chiffre qui décide si le coupe-parole est activable, et à quel seuil ;
- **le temps passé à attendre `END_SILENCE`** : une étape de plus dans `mesures.py`, pour savoir ce que
  l'étape 3 ferait gagner avant de l'écrire ;
- **le coût processeur d'un AEC sur le Pi**, en même temps que le visage à 30 ips et le suivi Kinect — pas en
  isolé, et pas à 3 h du matin quand le Pi est froid (c'est la leçon de `pi/tracker.py`) ;
- **combien de fois Greg coupe la parole à Bulle**, ou essaie : aujourd'hui, ça ne laisse aucune trace. Les
  signaux non verbaux (`cerveau.signaux_non_verbaux`) commencent à en garder de quoi le voir — un énoncé
  arrivé juste après une réponse, c'est souvent exactement ça.
