"""Isolation des tests unitaires : aucun réseau, aucune carte graphique, aucune base de production.

`journal.py` crée sa base SQLite AU MOMENT DE L'IMPORT (`os.makedirs` + `executescript`). Si on ne détourne pas
`BULLE_DB` avant que `cerveau/server.py` soit importé, lancer les tests écrit dans `~/kinectface/etat/bulle.db` —
la vraie base de Bulle. On la détourne donc ici, dans le conftest, qui est chargé avant tout module de test.
"""
import os
import sys
import tempfile

ICI = os.path.dirname(os.path.abspath(__file__))
DEPOT = os.path.dirname(os.path.dirname(ICI))

os.environ["BULLE_DB"] = os.path.join(tempfile.mkdtemp(prefix="bulle-tests-"), "bulle.db")
# Les cartes du Pi se dessinent avec pygame (test_carte_rendu.py). « dummy » lui donne un pilote vidéo qui
# n'affiche rien : sans ça, importer carte.py sur une machine sans écran — la CI en est une — peut échouer.
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("BULLE_TUILES", os.path.join(tempfile.gettempdir(), "bulle-tests-tuiles"))
os.environ.setdefault("BULLE_PLANS", os.path.join(tempfile.gettempdir(), "bulle-tests-plans"))

# les modules du cerveau s'importent entre eux à plat (« import regles »), comme quand uvicorn tourne dans cerveau/
sys.path.insert(0, os.path.join(DEPOT, "cerveau"))
