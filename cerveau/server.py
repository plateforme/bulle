"""Cerveau de Bulle (VM .31) : audio → Whisper → LLM + outils → étiquette d'émotion → Kyutai TTS.

Réglages de comportement : config/regles.yaml (rechargé à chaud). Outils composés : outils_composes.py.
Journal : SQLite (journal.py) — échanges, retours de Greg, incidents, actions, tests à faire. Page de suivi : /suivi.

WebSocket /ws
  client → serveur : {"type": "audio", "awake": bool}  annonce l'énoncé suivant (awake = fenêtre de conversation ouverte)
                     binaire = un énoncé WAV (le client fait la détection de voix)
                     {"type": "text", "text": "..."}   question tapée (tests)
                     {"type": "simulation", "actif": true} outils à effet simulés (banc de tests, agent)
                     {"type": "reset"}                 oublie la conversation
  serveur → client : {"type": "transcript", "text"} | {"type": "ignored", "reason"} | {"type": "wake"}
                     {"type": "thinking"} | {"type": "tool", "name", "args", "demande", "redirige"}
                     « name » et « args » sont ce que le cerveau a EXÉCUTÉ ; « demande » ce que le modèle
                     avait écrit, et « redirige » dit si les deux diffèrent.
                     {"type": "emotion", "emotion"}
                     {"type": "carte", "carte": {...} | null}  à afficher sur la TV à côté du visage (cartes.py)
                     {"type": "sentence", "text"} suivi d'un message binaire WAV (24 kHz mono)
                     {"type": "done", "echange": id} | {"type": "error", "message"}
"""
import asyncio, datetime, difflib, json, logging, os, re, time
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect

import cartes
import journal
import mesures
import plan
import outils_composes as oc
import regles
import selection_outils

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("LLM_MODEL", "gpt-oss:20b-32k")
THINK = os.environ.get("LLM_THINK", "low")
STT = os.environ.get("STT_URL", "http://localhost:8793/v1/audio/transcriptions")
TTS = os.environ.get("TTS_URL", "http://localhost:8791/v1/audio/speech")
TTS_VOICE = os.environ.get("TTS_VOICE", "")
OWUI = os.environ.get("OWUI_URL", "http://localhost:3000")
OWUI_KEY = os.environ.get("OPENWEBUI_API_KEY", "")
PRESET = os.environ.get("OWUI_PRESET", "voix")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Montreal"))
TOOL_SERVERS = [u for u in os.environ.get("TOOL_SERVERS",
                "http://localhost:8792,http://localhost:8795,http://localhost:8796,http://localhost:8801,http://localhost:9837").split(",") if u]


def _envv(name, default=""):
    """systemd garde les commentaires de fin de ligne du fichier d'environnement : on les retire."""
    return re.split(r"\s+#", os.environ.get(name, default), 1)[0].strip().strip('"').strip("'") or default


HA_URL = _envv("HA_URL", "http://192.0.2.15:8123").rstrip("/")
HA_TOKEN = _envv("HA_TOKEN")
SPOTIFY_PLAYBACK = {"play", "play_search", "pause", "next_track", "previous_track", "set_volume", "save_tracks", "transfer_playback"}
EMOTIONS = ["neutre", "joie", "tendre", "clin", "surprise", "curieux", "serieux", "desole"]
TAG = re.compile(r"^\s*\[([a-zA-Z_]+)\]\s*")
ANY_TAG = re.compile(r"\[(?:%s)\]" % "|".join(EMOTIONS), re.I)
SENT_END = re.compile(r"(.+?[.!?…:;])(\s+|$)", re.S)
SENT_COMMA = re.compile(r"(.{60,}?,)(\s+)", re.S)
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

log = logging.getLogger("cerveau"); logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
clients = set()                                  # connexions /ws en cours — publié en jauge sur /metrics


@asynccontextmanager
async def cycle_de_vie(_app):
    """Sans jeton, on refuse de démarrer — on ne démarre JAMAIS ouvert sur le réseau.

    Le cerveau écoute sur 0.0.0.0 et pilote une maison habitée : un objet connecté compromis sur le même réseau
    suffirait à allumer, éteindre, écouter. Le contrôle est ici plutôt qu'à l'import, pour que les tests
    unitaires puissent importer ce module sans secret.
    """
    if not regles.jeton():
        raise RuntimeError(
            "aucun jeton : le cerveau refuse de démarrer ouvert sur le réseau. "
            f"Renseigner BULLE_JETON, ou écrire le secret dans {regles.JETON_FICHIER} (chmod 600).")
    yield


app = FastAPI(title="Cerveau de Bulle", lifespan=cycle_de_vie)

# Le relais nginx devant Ollama ne journalise que l'IP et l'agent : sans agent à nous, Bulle arrivait en
# « python-httpx » depuis ::1, que le collecteur ia-usage range dans « système / scripts et tests » — un bac
# exclu du tableau de bord. Bulle y était donc invisible (constaté le 21/09/2026). L'agent du banc est distinct :
# le temps GPU des tests de nuit n'est pas celui du salon, et les mélanger fausserait les deux.
AGENT_HTTP = "Bulle/1.0 (cerveau; salon)"
AGENT_HTTP_BANC = "Bulle/1.0 (cerveau; banc)"
http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), headers={"User-Agent": AGENT_HTTP})


def entete_prompt():
    nom = regles.c("nom", "Bulle")
    return ("\n\nTu t'appelles " + nom + ". Tu t'exprimes à travers un visage animé affiché sur la TV du salon ; Greg te parle à voix haute "
            "depuis la pièce. Commence CHAQUE réponse finale par une seule étiquette d'émotion entre crochets, qui reflète le ton de ta "
            "réponse, choisie parmi : " + ", ".join(f"[{e}]" for e in EMOTIONS) + ". Exemple : « [joie] Il fait 22 °C et grand soleil. » "
            "Choisis [neutre] pour une information banale, [joie] pour une bonne nouvelle, [tendre] pour un remerciement ou un compliment, "
            "[clin] pour une plaisanterie, [surprise], [curieux] quand tu poses une question, [serieux] pour un avertissement, "
            "[desole] quand tu ne peux pas aider ou qu'une action échoue. L'étiquette n'est pas lue à voix haute. "
            "Aucune autre étiquette ailleurs dans la réponse. " + " ".join(regles.c("consignes", [])))


def entete_memoire():
    """Ce que Greg a demandé de retenir, relu à chaque échange (outil `retenir`, table `memoire`).

    Injecté en entier plutôt que cherché : à quelques dizaines de faits, un index vectoriel coûterait un
    aller-retour de plus sur la carte pour choisir entre trente phrases courtes. Quand ça ne tiendra plus,
    `memoire.max` le dira — c'est lui qu'il faudra remplacer par une recherche, pas ce bloc.
    """
    r = regles.c("memoire", {}) or {}
    if not r.get("actif", True): return ""
    try:
        faits = journal.souvenirs(limite=int(r.get("max", 40)))
    except Exception as e:                       # une mémoire illisible ne doit jamais rendre Bulle muette
        log.warning("mémoire illisible : %s", e); return ""
    if not faits: return ""
    return ("\n\nCe que Greg t'a demandé de retenir, et que tu tiens pour vrai sans avoir à le vérifier : "
            + " ".join("— " + f["fait"].rstrip(".") + "." for f in faits)
            + " Ne récite cette liste que s'il la demande (outil lister_souvenirs).")


# ---------------------------------------------------------------- client MCP minimal
class MCPServer:
    """HTTP « streamable », JSON-RPC : initialize, tools/list, tools/call."""

    def __init__(self, url, key=None):
        self.url, self.sid = url, None
        self.h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if key: self.h["Authorization"] = "Bearer " + key

    @staticmethod
    def _parse(r):
        if "text/event-stream" in r.headers.get("content-type", ""):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    d = json.loads(line[5:])
                    if "id" in d: return d
            return {}
        return r.json() if r.content else {}

    async def _rpc(self, method, params=None, _retry=True):
        h = dict(self.h, **({"Mcp-Session-Id": self.sid} if self.sid else {}))
        r = await http.post(self.url, headers=h, json={"jsonrpc": "2.0", "id": int(time.time() * 1000) % 10**9,
                                                       "method": method, "params": params or {}}, timeout=45)
        if r.status_code in (400, 404) and self.sid and _retry:   # session expirée : on la rouvre
            await self.init(); return await self._rpc(method, params, False)
        r.raise_for_status()
        d = self._parse(r)
        if "error" in d: raise RuntimeError(d["error"].get("message", str(d["error"])))
        return d.get("result", {})

    async def init(self):
        self.sid = None
        r = await http.post(self.url, headers=self.h, timeout=20, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bulle", "version": "1"}}})
        r.raise_for_status()
        self.sid = r.headers.get("mcp-session-id")
        h = dict(self.h, **({"Mcp-Session-Id": self.sid} if self.sid else {}))
        await http.post(self.url, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)

    async def list_tools(self):
        return (await self._rpc("tools/list")).get("tools", [])

    async def call(self, name, args):
        res = await self._rpc("tools/call", {"name": name, "arguments": args})
        txt = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return json.dumps({"erreur": txt}, ensure_ascii=False) if res.get("isError") else txt


async def ha_call(method, path, data=None):
    r = await http.request(method, HA_URL + path, json=data, timeout=15,
                           headers={"Authorization": "Bearer " + HA_TOKEN, "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json() if r.content else {}


# ---------------------------------------------------------------- contexte des outils composés
class Ctx:
    def __init__(self, tools, said="", sim=False, session=None):
        self.tools, self.said, self.sim, self.memo, self.session = tools, said, sim, tools.memo, session

    async def envoyer(self, msg):
        """Message direct au client (Pi) : mode nuit, etc."""
        if self.session: await self.session.send(msg)

    async def outil(self, name, args, mcp=False):
        if mcp:
            srv = self.tools.mcp_srv(name)
            if not srv: raise RuntimeError(f"outil MCP indisponible : {name}")
            if self.sim and not self.tools.lecture_seule(name, args):
                return json.dumps({"ok": True, "simulation": True})
            return await srv.call(name, args)
        return (await self.tools.call(name, args, self.said, self.sim))[0]

    async def json(self, name, args, mcp=False):
        return json.loads(await self.outil(name, args, mcp))

    async def ha(self, method, path, data=None):
        if self.sim and method.upper() != "GET":
            log.info("[simulation] HA %s %s %s", method, path, json.dumps(data, ensure_ascii=False)[:120])
            return {"simulation": True}
        return await ha_call(method, path, data)

    async def spotify(self, real, args):
        srv = self.tools.mcp_srv(real)
        if not srv: raise RuntimeError("Spotify indisponible")
        if self.sim:
            return json.dumps({"ok": True, "simulation": True})
        return await self.tools.spotify_call(srv, real, dict(args))

    @staticmethod
    def ok(fait="", non_fait=(), **extra):
        return json.dumps(dict({"ok": True, "fait": fait, "non_fait": list(non_fait)}, **extra), ensure_ascii=False)

    @staticmethod
    def non_fait(raison, **extra):
        return json.dumps(dict({"ok": False, "fait": "", "non_fait": [raison]}, **extra), ensure_ascii=False)


TWENTY_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "is", "ilike", "like", "startsWith", "containsAny"}
# Paramètres que le LLM emprunte à un autre outil (« mets le volume à 40 » → volume_pct de commande_media).
# Corrigé AVANT l'appel : le message « tool » et le journal doivent montrer ce qui a vraiment été exécuté.
ALIAS_PARAMS = {"volume_musique": {"volume_pct": "niveau", "volume": "niveau", "niveau_pct": "niveau",
                                   "pourcentage": "niveau", "delta": "changement"}}
# commande_media sur l'ampli renvoie un 500 et la consigne « jamais pour la musique » ne tient pas : on redirige.
MEDIA_MUSIQUE = {"pause": "pause", "stop": "pause", "arreter": "pause", "arrêter": "pause", "play": "play",
                 "lecture": "play", "reprendre": "play", "resume": "play", "suivant": "next_track",
                 "next": "next_track", "precedent": "previous_track", "previous": "previous_track"}


def twenty_intervalles(arguments):
    """Twenty refuse deux opérateurs sur un même champ (« Filter for field "dueAt" must have exactly one operator »).

    Or le LLM écrit naturellement un intervalle ainsi pour « la semaine prochaine ». On le réécrit en « and »,
    la seule forme que l'API accepte. Les champs composites ({"name": {"firstName": …}}) ne sont pas touchés.
    """
    sup = []
    for champ, cond in list(arguments.items()):
        if isinstance(cond, dict) and len(cond) > 1 and all(k in TWENTY_OPS for k in cond):
            sup += [{champ: {k: v}} for k, v in cond.items()]
            arguments.pop(champ)
    if sup:
        arguments["and"] = list(arguments.get("and") or []) + sup
    return arguments


# ---------------------------------------------------------------- catalogue d'outils
class Tools:
    def __init__(self):
        self.specs, self.routes, self.loaded, self.mcp, self.memo = [], {}, 0.0, {}, {}

    @staticmethod
    def _resolve(schema, doc):
        if isinstance(schema, dict):
            if "$ref" in schema:
                node = doc
                for part in schema["$ref"].lstrip("#/").split("/"):
                    node = node[part]
                return Tools._resolve(node, doc)
            return {k: Tools._resolve(v, doc) for k, v in schema.items()}
        if isinstance(schema, list):
            return [Tools._resolve(v, doc) for v in schema]
        return schema

    def lecture_seule(self, name, args):
        if regles.correspond(name, regles.c("lecture_seule", [])): return True
        if name in ("twenty_execute_tool", "execute_tool"):
            inner = str(args.get("toolName") or "")
            return any(inner.startswith(p) for p in regles.c("twenty_lecture", []))
        return False

    async def load(self, force=False):
        if not force and time.time() - self.loaded < 300 and self.specs:
            return
        specs, routes = [], {}
        exclus = set(regles.c("outils_exclus", []))
        for base in TOOL_SERVERS:
            try:
                doc = (await http.get(base.rstrip("/") + "/openapi.json", timeout=5)).json()
            except Exception as e:
                log.warning("serveur d'outils %s injoignable : %s", base, e); continue
            for path, methods in doc.get("paths", {}).items():
                for method, op in methods.items():
                    nom = path.strip("/").split("/")[-1]
                    if nom in exclus or nom in ("health", ""): continue
                    props, required = {}, []
                    for prm in op.get("parameters", []):
                        prm = self._resolve(prm, doc)
                        props[prm["name"]] = {k: v for k, v in prm.get("schema", {}).items() if k != "title"}
                        if prm.get("description"): props[prm["name"]]["description"] = prm["description"]
                        if prm.get("required"): required.append(prm["name"])
                    body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
                    if body:
                        body = self._resolve(body, doc)
                        props.update(body.get("properties", {})); required += body.get("required", [])
                    specs.append({"type": "function", "function": {"name": nom, "description": (op.get("description") or op.get("summary") or "")[:900],
                                                                   "parameters": {"type": "object", "properties": props, "required": required}}})
                    routes[nom] = (base.rstrip("/") + path, method, bool(body))
        mcp = {}
        try:
            cfg = (await http.get(f"{OWUI}/api/v1/configs/tool_servers", headers={"Authorization": "Bearer " + OWUI_KEY},
                                  timeout=5)).json().get("TOOL_SERVER_CONNECTIONS", [])
        except Exception as e:
            cfg = []; log.warning("configuration MCP d'Open WebUI illisible : %s", e)
        gardes = regles.c("mcp", {})
        for c in cfg:
            sid = c.get("info", {}).get("id")
            if c.get("type") != "mcp" or sid not in gardes or not c.get("config", {}).get("enable", True): continue
            prefix, keep = gardes[sid].get("prefixe", ""), set(gardes[sid].get("garder", []))
            srv = MCPServer(c["url"].replace("host.docker.internal", "localhost"), c.get("key") if c.get("auth_type") == "bearer" else None)
            try:
                await srv.init()
                for t in await srv.list_tools():
                    if sid == "spotify" and t["name"] == "get_saved_tracks":
                        mcp["__get_saved_tracks"] = (srv, t["name"])          # utilisé par les outils composés, pas exposé
                    if t["name"] not in keep: continue
                    specs.append({"type": "function", "function": {"name": prefix + t["name"], "description": (t.get("description") or "")[:900],
                                                                   "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})
                    mcp[prefix + t["name"]] = (srv, t["name"])
            except Exception as e:
                log.warning("serveur MCP %s injoignable : %s", sid, e)
        self.mcp, self.routes = mcp, routes
        dispo = set(routes) | {r for _, r in mcp.values()} | ({"spotify"} if any(r == "play" for _, r in mcp.values()) else set()) | (
            {"ha"} if HA_TOKEN else set())
        ctx = Ctx(self)
        for o in oc.OUTILS.values():
            if not set(o["necessite"]) <= dispo: continue
            params = o["parametres"]
            if callable(params):
                try: params = await params(ctx)
                except Exception as e:
                    log.warning("paramètres de %s indisponibles : %s", o["nom"], e); continue
            # un outil composé MASQUE la route du même nom (c'est déjà ce que fait call()) : sinon le LLM
            # voit deux fois « eteindre_tout », avec deux descriptions différentes
            specs = [sp for sp in specs if sp["function"]["name"] != o["nom"]]
            specs.append({"type": "function", "function": {"name": o["nom"], "description": o["description"],
                                                           "parameters": {"type": "object", "properties": params, "required": o["requis"]}}})
        self.specs, self.loaded = specs, time.time()
        log.info("%d outils chargés : %s", len(specs), ", ".join(f["function"]["name"] for f in specs))

    def noms(self):
        return {f["function"]["name"] for f in self.specs}

    async def call(self, name, args, said="", sim=False, session=None):
        """→ (résultat texte, nom réellement appelé, arguments réellement passés).

        Elle ne rend plus « la redirection éventuelle » mais CE QU'ELLE A FAIT, et la nuance a coûté cher :
        l'appelant reconstituait les arguments à la devinette (`args.get("arguments", {})` dès qu'il y avait
        redirection). Cette devinette ne vaut que pour le déballage d'un appel emballé par Twenty ; pour
        toutes les AUTRES redirections, l'appelant envoyait un dictionnaire VIDE — au client, au journal, et
        au banc, qui juge l'agent de nuit sur ces arguments-là.

        Symétriquement, une réparation qui se contentait de renommer (table d'alias, préfixe déformé) restait
        invisible : `mesures.outil()` recevait le nom halluciné, qui passe le filtre des étiquettes et ouvre
        sa propre série Prometheus. Soixante suffisent à saturer le budget.
        """
        if name not in self.noms():        # nom inventé par le LLM (« displayer » pour afficher) : table dans regles.yaml
            alias = (regles.c("alias_outils", {}) or {}).get(name)
            if alias in self.noms():
                log.info("nom d'outil inventé : %s → %s", name, alias); name = alias
        # « twelve_learn_tools », « twelve_lancer_scene » : le modèle écrit « twelve » là où le catalogue dit
        # « twenty », et colle même ce préfixe à des outils maison. Réparer le NOM est sûr — c'est une égalité,
        # pas une devinette — et ça doit passer AVANT la reconnaissance par signature, qui avait envoyé
        # « twelve_lancer_scene » sur jouer_playlist le 21/09 : 683 titres au lieu d'une scène. Une table
        # d'alias ne suffit pas ici : elle se remplit un nom à la fois, et le suivant échoue quand même
        # (« twelve_learn_tools » → « je ne peux pas arrêter le rappel », 21/09).
        if name not in self.noms():
            for deforme, vrai in (regles.c("prefixes_outils_deformes", {}) or {}).items():
                if not name.startswith(deforme): continue
                nu = name[len(deforme):]
                essai = next((e for e in (vrai + nu, nu) if e in self.noms()), None)
                if essai:
                    log.info("préfixe d'outil déformé (%s) → %s", name, essai); name = essai
                break
        # Nom méconnaissable et absent de la table d'alias (« twelve_instructions », « twelve? ») : les ARGUMENTS,
        # eux, sont justes. Si un seul outil composé a exactement cette signature, c'est lui — sinon le LLM perd un
        # aller-retour à se corriger tout seul, et Greg attend (8,7 s au lieu de 3, vu le 21/09).
        if name not in self.noms() and args and not (args.get("toolName") or args.get("tool_name")):
            cles = set(args)
            candidats = [n for n, o in oc.OUTILS.items()
                         if n in self.noms() and isinstance(o["parametres"], dict)
                         and cles <= set(o["parametres"]) and set(o["requis"]) <= cles]
            # mode_club construit ses paramètres à la volée (les palettes viennent de Home Assistant) : il ne peut
            # pas être comparé, donc on renonce plutôt que de risquer de lui prendre son appel (« actif » aurait
            # désigné mode_nuit à tort).
            flou = [n for n, o in oc.OUTILS.items()
                    if n in self.noms() and not isinstance(o["parametres"], dict) and set(o["requis"]) <= cles]
            if len(candidats) == 1 and not flou:
                log.info("nom d'outil méconnaissable (%s) : retrouvé à sa signature → %s", name, candidats[0])
                name = candidats[0]
        # le LLM « emballe » parfois un de nos outils dans l'aiguillage de Twenty (twenty_execute_tool(toolName="pause"))
        # ou déforme son nom (« twelve_execute_tool », « twelve? ») : on redirige vers le bon outil
        if name not in self.noms() and (args.get("toolName") or args.get("tool_name")):
            name = "twenty_execute_tool"
        # … parfois deux fois de suite : twenty_execute_tool(toolName="twenty_execute_tool", arguments={toolName: "pause"})
        while name == "twenty_execute_tool" and (args.get("toolName") or args.get("tool_name")) in ("twenty_execute_tool", "execute_tool"):
            log.info("appel emballé deux fois : on déballe")
            args = args.get("arguments") or {}
        if name == "twenty_execute_tool":
            inner = args.get("toolName") or args.get("tool_name") or ""
            if inner in self.noms() - {"twenty_execute_tool", "twenty_get_tool_catalog", "twenty_learn_tools"}:
                log.info("appel emballé redirigé : %s", inner)
                name, args = inner, (args.get("arguments") or {})
            elif isinstance(args.get("arguments"), dict):
                args = dict(args, arguments=twenty_intervalles(dict(args["arguments"])))

        # --- corrections d'appel, APRÈS le déballage : sinon elles portent sur « twenty_execute_tool » et ratent
        # la vraie cible (vu le 20/09 : « C'est parti. » arrivait emballé, la garde play_search ne voyait rien).
        for faux, vrai in (ALIAS_PARAMS.get(name) or {}).items():
            if args.get(faux) is not None and args.get(vrai) is None:
                # La correction porte sur le dictionnaire courant, qui est celui que `call()` rapporte comme
                # arguments réellement passés. Avant le 21/09 elle comptait sur une mutation SUR PLACE du
                # dictionnaire de l'appelant — ce qui ne tenait déjà plus après un déballage Twenty, où `args`
                # a été rebindé quelques lignes plus haut.
                args[vrai] = args.pop(faux)
        # « C'est parti. » → play_search(query="") : chercher le vide n'a aucun sens et fait jouer n'importe quoi
        if name == "play_search" and not str(args.get("query") or "").strip() and "play" in self.noms():
            log.info("play_search sans recherche → play")
            name, args = "play", {}
        # « Augmente le volume de 10 % » → niveau=10 : le LLM confond variation et niveau absolu, et le volume
        # TOMBE à 10 %. La phrase le dit sans ambiguïté, on la relit plutôt que d'espérer.
        if name == "volume_musique" and args.get("niveau") is not None and not args.get("changement"):
            verbe = re.search(r"\b(augmente|monte|baisse|diminue|r[ée]duis)\w*", said or "", re.I)
            try: valeur = int(args["niveau"])
            except (TypeError, ValueError): valeur = None
            if verbe and valeur is not None and re.search(r"\bde\s+%d\b" % valeur, said or ""):
                signe = -1 if re.match(r"(baisse|diminue|r[ée]duis)", verbe.group(1), re.I) else 1
                args["changement"], args["niveau"] = signe * valeur, None
                log.info("volume relatif : « %s » → changement %+d", (said or "")[:40], args["changement"])
        # commande_media ne sert QUE si Greg a parlé de la TV : sinon un « arrête » vise la musique, sur l'ampli
        if name == "commande_media" and MEDIA_MUSIQUE.get(str(args.get("action") or "").lower()) in self.noms():
            mots_tv = regles.c("musique", {}).get("mots_tv", "(?:^|[^a-z])(?:tv|t[ée]l[ée])")
            if not re.search(mots_tv, said or "", re.I):
                cible = MEDIA_MUSIQUE[str(args["action"]).lower()]
                log.info("commande_media sans « TV » dans la demande : %s → %s", args.get("action"), cible)
                name, args = cible, {}
        if name in oc.OUTILS and name in self.noms():
            try: return await oc.OUTILS[name]["fn"](Ctx(self, said, sim, session), dict(args)), name, args
            except Exception as e:
                log.exception("outil composé %s", name)
                return json.dumps({"erreur": str(e)}, ensure_ascii=False), name, args
        if sim and not self.lecture_seule(name, args):
            log.info("[simulation] %s(%s)", name, json.dumps(args, ensure_ascii=False)[:120])
            return json.dumps({"ok": True, "simulation": True, "fait": f"{name} (simulé)"}, ensure_ascii=False), name, args
        if name in self.mcp:
            srv, real = self.mcp[name]
            try:
                # pas de troncature ici : un résultat coupé en plein JSON casse les outils composés et les cartes.
                # On coupe au moment de le donner au LLM (answer), là où la limite a un sens.
                if real in SPOTIFY_PLAYBACK:
                    return await self.spotify_call(srv, real, dict(args)), name, args
                return await srv.call(real, args), name, args
            except Exception as e:
                return json.dumps({"erreur": str(e)}, ensure_ascii=False), name, args
        if name not in self.routes:
            return json.dumps({"erreur": f"outil inconnu : {name}"}, ensure_ascii=False), name, args
        url, method, has_body = self.routes[name]
        # Une lecture qui échoue mérite une seconde chance : Open-Meteo a renvoyé 503 pendant trois questions
        # météo d'affilée (20/09), et Bulle répondait sans carte. On ne réessaie QUE les GET : rejouer un
        # « allumer » ou un « envoyer_courriel » les exécuterait deux fois.
        for essai in range(2 if method == "get" else 1):
            try:
                if method == "get":
                    r = await http.get(url, params={k: v for k, v in args.items() if v is not None}, timeout=60)
                else:
                    r = await http.request(method.upper(), url, json=args if has_body else None,
                                           params=None if has_body else args, timeout=60)
            except Exception as e:
                if essai: return json.dumps({"erreur": str(e)}, ensure_ascii=False), name, args
                await asyncio.sleep(0.6); continue
            if r.status_code >= 500 and not essai:
                log.info("%s a répondu %s : nouvel essai", name, r.status_code)
                await asyncio.sleep(0.6); continue
            if r.status_code >= 400 and not r.text.strip().startswith("{"):
                # sinon le LLM reçoit « Internal Server Error » en texte brut et invente parfois une réponse
                return json.dumps({"erreur": f"{name} a échoué ({r.status_code})", "detail": r.text[:160]},
                                  ensure_ascii=False), name, args
            return r.text[:6000], name, args

    async def spotify_call(self, srv, real, args):
        """Identifiant d'appareil inventé par le LLM → retiré ; aucun appareil actif → repli sur l'appareil par défaut."""
        try: devices = json.loads(await srv.call("get_devices", {}))
        except Exception: devices = []
        ids = {d.get("id") for d in devices if isinstance(d, dict)}
        if args.get("device_id") not in ids: args.pop("device_id", None)
        res = await srv.call(real, args)
        if "appareil" in res.lower() and "actif" in res.lower() and "device_id" not in args:
            dflt_nom = regles.c("musique", {}).get("appareil_par_defaut", "CXN100").lower()
            dflt = next((d["id"] for d in devices if isinstance(d, dict) and d.get("name", "").lower() == dflt_nom), None)
            if dflt:
                log.info("aucun appareil Spotify actif : repli sur %s", dflt_nom)
                res = await srv.call(real, dict(args, device_id=dflt))
        return res


def _mcp_srv(self, real):
    if real == "get_saved_tracks" and "__get_saved_tracks" in self.mcp:
        return self.mcp["__get_saved_tracks"][0]
    return next((srv for srv, r in self.mcp.values() if r == real), None)


Tools.mcp_srv = _mcp_srv
tools = Tools()
_prompt_cache = {"t": 0.0, "text": ""}


async def system_prompt(sim=False):
    if time.time() - _prompt_cache["t"] > 300:
        try:
            r = await http.get(f"{OWUI}/api/v1/models/model", params={"id": PRESET}, headers={"Authorization": "Bearer " + OWUI_KEY}, timeout=5)
            _prompt_cache.update(t=time.time(), text=r.json()["params"]["system"])
        except Exception as e:
            log.warning("prompt du préréglage %s indisponible : %s", PRESET, e)
    now = datetime.datetime.now(TZ)
    txt = _prompt_cache["text"] or "Tu es un assistant vocal. Réponds en français, en phrases courtes, sans markdown."
    txt = txt.replace("{{CURRENT_WEEKDAY}}", JOURS[now.weekday()]).replace("{{CURRENT_DATETIME}}", now.strftime("%Y-%m-%d %I:%M %p"))
    # Pas de souvenirs pendant le banc : ils rendraient le juge dépendant de ce que Greg a fait retenir ce
    # mois-ci, et une correction pourrait être rejetée par un souvenir ajouté la veille.
    return txt + entete_prompt() + ("" if sim else entete_memoire())


def _premier_mot_proche(brut):
    """Le premier mot de l'énoncé, mais SEULEMENT s'il ressemble au nom de Bulle — sinon None.

    C'est tout ce qu'on garde d'une phrase qui n'était pas pour elle. Un « Boule, ferme la lumière » mal
    transcrit vaut d'être relevé, il ira dans nom_mal_entendu ; le reste de ce qui se dit au salon ne nous
    regarde pas. « les », « quelle »… ressemblent de loin à « bulle » : on exige une vraie proximité.
    """
    premier = oc._norm(re.split(r"[\s,.!?]+", (brut or "").strip() + " ")[0])
    nom = oc._norm(regles.c("nom", "Bulle"))
    if len(premier) >= 4 and premier != nom and difflib.SequenceMatcher(None, premier, nom).ratio() >= 0.7:
        return premier
    return None


# Ce que le Pi a le droit de nous dire d'un énoncé SANS en écouter les mots (compagnon.py). Liste fermée et
# valeurs forcées en nombres : c'est la même précaution que pour les noms d'outils dans mesures.py. Le Pi est
# une machine de confiance, mais ce champ finit en base et y reste des jours — une clé libre suffirait à ce
# qu'une version future du client y glisse un bout de transcription sans que personne le remarque.
SIGNAUX = ("duree_s", "niveau", "vad", "nettete", "retards", "angle_kinect", "distance_m", "presence",
           "fuite")     # fuite : ce que le haut-parleur renvoie dans les micros quand Bulle parle (full-duplex)


def _signaux_propres(d):
    """→ les signaux non verbaux, réduits aux clés connues et à des nombres. Jamais de texte, par construction."""
    if not isinstance(d, dict): return {}
    out = {}
    for cle in SIGNAUX:
        v = d.get(cle)
        if isinstance(v, bool):
            out[cle] = int(v)
        elif isinstance(v, (int, float)):
            out[cle] = round(float(v), 3)
        elif isinstance(v, (list, tuple)) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            out[cle] = [round(float(x), 3) for x in v[:8]]
    return out


def confiance(reponse):
    """La confiance de Whisper dans ce qu'il vient d'écrire : moyenne des `avg_logprob`, pondérée par la durée.

    Pondérée, parce qu'un « oui » d'un dixième de seconde pèserait autant qu'une phrase de trois secondes.
    Rend None quand la passerelle n'a rien à en dire — transcription rejetée en amont, format simple, segments
    sans mesure. Un énoncé qu'on n'a PAS mesuré ne doit jamais être écarté sur une valeur inventée.
    """
    segs = [x for x in (reponse.get("segments") or [])
            if isinstance(x, dict) and isinstance(x.get("avg_logprob"), (int, float))]
    duree = 0.0
    total = 0.0
    for x in segs:
        try: d = max(0.01, float(x.get("end", 0)) - float(x.get("start", 0)))
        except (TypeError, ValueError): continue
        duree += d; total += float(x["avg_logprob"]) * d
    return total / duree if duree > 0 else None


def douteuse(conf):
    """Faut-il faire répéter plutôt qu'agir ?

    Le 21/09, « Jour à du 9 à la nuit. » a créé un rappel pour 21 h : rien n'empêchait Bulle d'agir sur une
    phrase qu'elle n'avait manifestement pas comprise. Le signal existait pourtant — la passerelle STT demande
    déjà `verbose_json` à Whisper — mais le cerveau ne gardait que le texte.

    Deux refus explicites, tous les deux du côté prudent : sans mesure, on ne bloque pas ; sans seuil réglé,
    on ne bloque pas non plus. Le défaut à corriger est d'agir à tort, pas de se taire à tort — mais refuser
    une bonne phrase reste une régression, et le seuil se règle à chaud.
    """
    seuil = regles.c("confiance_stt_min")
    if conf is None or seuil is None: return False
    try: return float(conf) < float(seuil)
    except (TypeError, ValueError): return False


def _signaux_a_garder(raison, proche, depuis_reponse):
    """Faut-il garder les signaux de cet énoncé ignoré ?

    Non, par défaut. Le salon est une pièce habitée : d'une conversation qui ne s'adressait visiblement pas à
    Bulle, il ne doit rester que l'heure, la longueur et la raison — la géométrie de qui parlait, d'où et
    pendant combien de temps en dirait déjà trop sur une soirée entre amis.

    Oui pour les ratés de peu, et eux seuls : un premier mot qui ressemble au nom, ou un énoncé arrivé juste
    après une réponse (la fenêtre venait de se fermer). Ce sont les deux cas que l'analyste relève déjà
    (`ignore_nom_probable`, `ignore_juste_apres_reponse`), et les seuls où savoir POURQUOI Bulle n'a pas
    répondu demande autre chose qu'une longueur. C'est aussi là que se trouve la frontière de décision : le
    reste du salon est trivialement négatif et n'apprendrait rien à personne.
    """
    if not (regles.c("signaux_non_verbaux", {}) or {}).get("actif"): return False
    # « hallucination » : du bruit que Whisper a mis en mots, personne ne parlait à personne.
    # « transcription_douteuse » : l'énoncé était pour Bulle, elle n'a pas répondu — la distance, le niveau et
    # la fuite sont exactement ce qu'il faut pour régler le seuil sur le vrai salon plutôt qu'au jugé.
    if raison in ("hallucination", "transcription_douteuse"): return True
    seuil = float((regles.c("signaux_non_verbaux", {}) or {}).get("apres_reponse_s", 30))
    return bool(proche) or (0 < depuis_reponse <= seuil)


def _est_indice(brut):
    """Whisper recrache son indice sur du bruit — parfois en entier, parfois un morceau seulement.

    Le 20/09, « CXN100, Home Assistant, Montréal, mode club, titres likés » (l'indice sans son premier mot) a été
    pris pour une demande et Bulle a lancé la musique. On ignore donc tout énoncé CONTENU dans l'indice.
    """
    mots = lambda t: " ".join(re.sub(r"[^a-z0-9]+", " ", oc._norm(t)).split())
    b, indice = mots(brut), mots(regles.c("indice_stt", ""))
    return bool(indice) and len(b) >= 16 and len(b.split()) >= 3 and b in indice


# ---------------------------------------------------------------- une conversation
def _sans_redite(history, text, seuil=0.86):
    """L'historique, privé du dernier échange s'il répondait DÉJÀ à la question qu'on repose.

    Greg redemande la météo deux minutes plus tard : le modèle retrouvait sa réponse dans l'historique et la
    resservait au mot près, sans rappeler l'outil — donc sans carte, et avec une température périmée (vu le
    21/09 à 18h45, et c'est aussi le « tu répètes deux fois les mêmes réponses » du 20/09). Reposer une question
    veut dire « vérifie », pas « répète ». On ne coupe QUE la paire concernée : le reste du fil sert encore aux
    questions de suite (« et demain ? »).
    """
    if len(history) < 2 or history[-2].get("role") != "user":
        return history
    avant = (history[-2].get("content") or "").strip().lower()
    apres = (text or "").strip().lower()
    if not avant or not apres:
        return history
    if difflib.SequenceMatcher(None, avant, apres).ratio() < seuil:
        return history
    log.info("question reposée (« %s ») : on retire la réponse précédente du fil", apres[:60])
    return history[:-2]


class Session:
    def __init__(self, ws: WebSocket):
        self.ws, self.history, self.last = ws, [], time.time()
        self.send_lock = asyncio.Lock()
        self.awake_next, self.sim, self.dernier_echange = False, False, None
        self.signaux, self.derniere_reponse = {}, 0.0    # signaux non verbaux du dernier énoncé, et quand Bulle a fini de parler
        self.muette = False          # vrai dès que la synthèse vocale a échoué : on n'annonce la panne qu'une fois
        self.t_recu, self.son_mesure = 0.0, True   # arrivée de l'énoncé, et si son délai jusqu'au son est déjà compté

    async def send(self, obj=None, data=None):
        async with self.send_lock:
            if obj is not None: await self.ws.send_text(json.dumps(obj, ensure_ascii=False))
            if data is not None and not self.sim: await self.ws.send_bytes(data)

    async def transcribe(self, wav):
        t0 = time.time()
        try:
            # verbose_json : la passerelle le demande déjà à Whisper et sait le renvoyer tel quel. Sans ce
            # mot, le cerveau ne voyait que le texte et jetait la confiance avec le reste.
            r = await http.post(STT, files={"file": ("u.wav", wav, "audio/wav")},
                                data={"language": "fr", "prompt": regles.c("indice_stt", ""),
                                      "response_format": "verbose_json"})
        except Exception:
            mesures.etape("transcription", time.time() - t0, ok=False); raise
        mesures.etape("transcription", time.time() - t0)
        j = r.json()
        return (j.get("text") or "").strip(), confiance(j)

    async def speak_worker(self, q: asyncio.Queue):
        while True:
            s = await q.get()
            if s is None: return
            if self.sim:                                     # banc de tests : pas de synthèse vocale
                await self.send({"type": "sentence", "text": s}); continue
            body = {"input": s, "response_format": "wav"}
            if TTS_VOICE: body["voice"] = TTS_VOICE
            t0 = time.time()
            try:
                r = await http.post(TTS, json=body)
                r.raise_for_status()
                mesures.etape("synthese", time.time() - t0); mesures.caracteres_dits(len(s))
                if not self.son_mesure:      # le délai ressenti : de la fin de l'énoncé au premier son
                    self.son_mesure = True
                    mesures.premier_son(time.time() - self.t_recu)
                await self.send({"type": "sentence", "text": s}, r.content)
            except Exception as e:
                # Sans ça, le client ne reçoit RIEN : ni son, ni texte, ni erreur — Bulle paraît cassée alors
                # qu'elle a répondu (vu le 21/09 : la synthèse avait changé de port pendant une migration).
                mesures.etape("synthese", time.time() - t0, ok=False)
                log.warning("TTS en échec : %s", e)
                await self.send({"type": "sentence", "text": s})
                if not self.muette:                       # une seule fois par échange : elle l'écrit faute de le dire
                    self.muette = True
                    await self.send({"type": "carte", "carte": {
                        "gabarit": "texte", "titre": "Je n'ai plus de voix", "texte": s[:110],
                        "note": "le serveur de synthèse ne répond pas", "duree": 30}})

    async def say(self, text, emotion="neutre"):
        """Réponse fixe, sans LLM."""
        await self.send({"type": "emotion", "emotion": emotion})
        q = asyncio.Queue(); w = asyncio.create_task(self.speak_worker(q))
        await q.put(text); await q.put(None); await w
        await self.send({"type": "done"})

    async def montrer(self, nom, args, resultat):
        """Carte issue d'un résultat d'outil : elle part pendant la réflexion, avant la première phrase."""
        if not cartes.autorisee(nom): return
        try:
            c = cartes.depuis_outil(nom, args, resultat)
        except Exception as e:                       # une carte ratée ne doit jamais casser une réponse
            log.warning("carte impossible pour %s : %s", nom, e); return
        if not c: return
        c.setdefault("duree", (regles.c("cartes", {}) or {}).get("duree_s", 15))
        log.info("carte %s « %s » (%d lignes) pour %s", c.get("gabarit"), c.get("titre", ""), len(c.get("items") or []), nom)
        await self.send({"type": "carte", "carte": c})

    def est_retour_negatif(self, text):
        t = oc._norm(text)
        return any(oc._norm(p) in t for p in regles.c("retour_negatif", []))

    async def answer(self, text, brut="", source="texte", nomme=False, eveille=False):
        if self.dernier_echange and self.est_retour_negatif(text):
            journal.retour(self.dernier_echange, text)
            log.info("retour négatif sur l'échange %s : %r", self.dernier_echange, text)
            await self.say(regles.c("retour_reponse", "D'accord, je le note."), "desole")
            return
        if time.time() - self.last > regles.c("oubli_apres_s", 600): self.history = []
        self.last = time.time()
        await tools.load()
        msgs = [{"role": "system", "content": await system_prompt(self.sim)}] + _sans_redite(self.history, text) \
            + [{"role": "user", "content": text}]
        q: asyncio.Queue = asyncio.Queue()
        worker = asyncio.create_task(self.speak_worker(q))
        t0, final, emotion_sent, emotion, trace = time.time(), "", False, "neutre", []
        mode, premiere = ("banc" if self.sim else "salon"), None
        appeles = set()      # outils déjà appelés dans cet échange : ils restent offerts aux tours suivants

        async def dire(phrase):
            """Met une phrase en file pour la voix, et retient quand la première est partie : c'est ce délai-là
            qu'on ressent comme la vitesse de Bulle, la suite se disant pendant que le modèle écrit encore."""
            nonlocal premiere
            if premiere is None: premiere = time.time() - t0
            await q.put(phrase)

        try:
            for rnd in range(10):  # tours d'outils ; au dernier, plus d'outils : réponse obligatoire
                content, calls, buf = "", [], ""
                # Au dernier tour, plus d'outils du tout : le modèle DOIT répondre. Avant ça, seulement ceux du
                # sujet (selection_outils) — c'est du préremplissage en moins sur une carte qu'on partage.
                offerts = [] if rnd >= 9 else selection_outils.choisir(tools.specs, text, appeles)
                if offerts: mesures.selection(len(offerts), len(tools.specs))
                t_llm = time.time()
                async with http.stream("POST", f"{OLLAMA}/api/chat", headers={"User-Agent": AGENT_HTTP_BANC if self.sim else AGENT_HTTP},
                        json={
                        "model": MODEL, "messages": msgs, "tools": offerts, "stream": True, "think": THINK,
                        "options": {"temperature": 0.3}}) as r:
                    async for line in r.aiter_lines():
                        if not line: continue
                        ch = json.loads(line)
                        m = ch.get("message", {})
                        calls += m.get("tool_calls") or []
                        piece = m.get("content") or ""
                        if not piece: continue
                        content += piece; buf += piece
                        if not emotion_sent:
                            mt = TAG.match(buf)
                            if mt:
                                emo = mt.group(1).lower()
                                emotion = emo if emo in EMOTIONS else "neutre"
                                await self.send({"type": "emotion", "emotion": emotion})
                                emotion_sent, buf = True, buf[mt.end():]
                            elif len(buf) > 24 or (buf.strip() and not buf.lstrip().startswith("[")):
                                await self.send({"type": "emotion", "emotion": "neutre"}); emotion_sent = True
                            else:
                                continue
                        while True:
                            ms = SENT_END.match(buf)
                            if not ms or len(ms.group(1)) < 12:
                                mc = SENT_COMMA.match(buf)  # longue phrase : on coupe à une virgule
                                if not mc: break
                                ms = mc
                            sent = ANY_TAG.sub("", ms.group(1)).strip()
                            buf = buf[ms.end():]
                            if sent: await dire(sent)
                # Le temps du GPU, et lui seul : les appels d'outils qui suivent partent sur le réseau, ils ne
                # coûtent rien au modèle. C'est ce découpage qui permet de comparer Bulle aux autres usagers.
                mesures.etape("reflexion", time.time() - t_llm, mode=mode)
                if calls:
                    msgs.append({"role": "assistant", "content": content, "tool_calls": calls})
                    for c in calls:
                        fn = c.get("function", {}); name, args = fn.get("name", ""), fn.get("arguments") or {}
                        if isinstance(args, str):
                            try: args = json.loads(args)
                            except ValueError: args = {}
                        # `fait` et `args_faits` sont ce que le cerveau a RÉELLEMENT exécuté, pas ce que le
                        # modèle avait demandé : c'est la seule version qui doive partir au client, au journal
                        # et aux mesures.
                        res, fait, args_faits = await tools.call(name, args, text, self.sim, session=self)
                        redirige = fait != name
                        appeles.update({name, fait})
                        await self.send({"type": "tool", "name": fait, "args": args_faits,
                                         "demande": name, "redirige": redirige})
                        await self.montrer(fait, args_faits, res)
                        ok = not re.search(r'"erreur"|"error"|Internal Server Error', res[:400])
                        mesures.outil(fait, ok)
                        trace.append({"nom": fait, "demande": name, "args": args_faits, "resultat": res[:400], "ok": ok})
                        log.info("outil %s(%s) → %s", name, json.dumps(args, ensure_ascii=False)[:120], res[:160].replace("\n", " "))
                        msgs.append({"role": "tool", "content": res[:6000], "tool_name": name})
                    continue
                rest = ANY_TAG.sub("", buf).strip()
                if rest: await dire(rest)
                final = TAG.sub("", content).strip()
                if not final:   # le LLM n'a rien produit : mieux vaut le dire que rester muet
                    final = "Pardon, je n'ai pas compris. Tu peux reformuler ?"
                    if not emotion_sent: await self.send({"type": "emotion", "emotion": "desole"})
                    await dire(final)
                    trace.append({"nom": "__reponse_vide__", "ok": False})
                break
        except (httpx.TimeoutException, httpx.RequestError) as e:
            # La 3090 et ses 8 cœurs sont partagés : une indexation lancée ailleurs peut les prendre plusieurs
            # minutes, et Bulle dépassait alors son délai SANS RIEN DIRE — le client affichait une mine désolée
            # et le salon voyait un visage figé (21/09 : « Bulle semble être coincée »). Une panne se dit.
            log.warning("le modèle n'a pas répondu (%s) : %s", type(e).__name__, e)
            mesures.etape("reflexion", time.time() - t_llm, mode=mode, ok=False)
            final = regles.c("phrase_debordee",
                             "Je suis débordée, la machine est prise. Redemande-moi dans un instant.")
            if not emotion_sent: await self.send({"type": "emotion", "emotion": "desole"})
            await dire(final)
            trace.append({"nom": "__modele_muet__", "args": {"erreur": type(e).__name__}, "ok": False})
        finally:
            await q.put(None)
            await worker
        self.derniere_reponse = time.time()      # sert à reconnaître l'énoncé arrivé juste après (voir _signaux_a_garder)
        n = regles.c("historique_tours", 6)
        self.history = (self.history + [{"role": "user", "content": text}, {"role": "assistant", "content": final}])[-2 * n:]
        duree = time.time() - t0
        eid = journal.echange(source=source, brut=brut or text, texte=text, nomme=int(nomme), eveille=int(eveille), outils=trace,
                              reponse=final, emotion=emotion, duree=round(duree, 2), simulation=int(self.sim),
                              signaux=self.signaux or None)
        if not self.sim: self.dernier_echange = eid
        mesures.echange("banc" if self.sim else source, duree, premiere)
        log.info("réponse en %.1f s : %s", duree, final[:200].replace("\n", " "))
        await self.send({"type": "done", "echange": eid})


@app.get("/plan/{cle}.png")
async def plan_png(cle: str, authorization: str = Header(default="")):
    """Vignette de plan d'une carte « lieu » — le Pi la télécharge sur le réseau local, jeton à l'appui."""
    from fastapi.responses import FileResponse
    if not regles.jeton_valide(authorization):
        raise HTTPException(status_code=401, detail="jeton absent ou invalide")
    chemin = os.path.join(plan.PLANS, re.sub(r"[^0-9a-f]", "", cle)[:32] + ".png")
    # le dessin est lancé en arrière-plan pour ne pas retarder la parole (Overpass met parfois 10 s) :
    # si le Pi arrive avant, on le fait patienter plutôt que de lui répondre « pas de plan »
    for _ in range(60):
        if os.path.exists(chemin): break
        await asyncio.sleep(0.5)
    else:
        raise HTTPException(status_code=404, detail="plan indisponible")
    return FileResponse(chemin, media_type="image/png")


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Ce que Bulle consomme, pour Prometheus (CT101) et le tableau de bord « Bulle » de Grafana.

    Ouvert, comme /health : des compteurs et des durées, rien de ce qui s'est dit au salon (voir mesures.py).
    Le jeton n'y est pas exigé parce qu'il faudrait alors le recopier sur CT101, une machine de plus à
    compromettre pour une page qui n'apprend rien à personne.
    """
    from fastapi.responses import PlainTextResponse
    mesures.modele(MODEL)
    return PlainTextResponse(mesures.rendu(), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/health")
async def health():
    await tools.load()
    return {"status": "ok", "model": MODEL, "tools": sorted(tools.noms()), "regles": os.path.realpath(regles.CHEMIN)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # Refus AVANT accept() : Starlette répond 403 à la poignée de main, rien n'est alloué, et un client qui
    # insiste ne voit jamais la différence entre « mauvais jeton » et « pas de service ici ».
    if not regles.jeton_valide(ws.headers.get("authorization")):
        log.warning("connexion refusée, jeton absent ou invalide : %s", ws.client)
        await ws.close(code=1008)
        return
    await ws.accept()
    s = Session(ws)
    clients.add(id(ws)); mesures.clients(len(clients))
    log.info("client connecté : %s", ws.client)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect": break
            try:
                if msg.get("bytes"):
                    s.t_recu, s.son_mesure = time.time(), False
                    t0 = time.time()
                    brut, conf = await s.transcribe(msg["bytes"])
                    if conf is not None: mesures.confiance(conf)
                    # Rien de ce qui a été transcrit n'est journalisé AVANT de savoir si c'était pour Bulle :
                    # sinon toute conversation du salon finit dans journalctl, qui la garde des semaines.
                    dt = time.time() - t0
                    awake, s.awake_next = s.awake_next, False
                    if not brut:
                        log.info("STT %.1f s : silence", dt)
                        mesures.ignore("silence")
                        await s.send({"type": "ignored", "reason": "silence"}); continue
                    wake, vocatif, vocatif_fin, mal = regles.regex_nom()
                    text = brut
                    hallu = next((h for h in regles.c("hallucinations", [])
                                  if re.search(h, oc._norm(brut), re.I)), None)
                    if not hallu and _est_indice(brut):
                        hallu = "indice_stt"
                    if hallu:
                        log.info("STT %.1f s : hallucination de Whisper ignorée (%s, %d caractères)", dt, hallu, len(brut))
                        journal.ignore("hallucination", len(brut), eveille=int(awake), detail=hallu,
                                       signaux=s.signaux if _signaux_a_garder("hallucination", None, 0) else None)
                        mesures.ignore("hallucination")
                        await s.send({"type": "ignored", "reason": "hallucination"}); continue
                    if mal.match(text):
                        text = mal.sub(regles.c("nom", "Bulle") + ", ", text, count=1)
                        log.info("nom mal transcrit, accepté : %r", text)
                    named = bool(wake.search(text))
                    if not (awake or named):
                        # Ce qui n'était pas pour Bulle ne laisse qu'une trace anonyme : la raison, la longueur, et
                        # le premier mot UNIQUEMENT s'il ressemble à son nom (c'est la seule chose à en apprendre).
                        proche = _premier_mot_proche(brut)
                        log.info("STT %.1f s : pas pour moi (%d caractères%s)", dt, len(brut),
                                 f", commence par « {proche} »" if proche else "")
                        garder = _signaux_a_garder("pas_nomme", proche, time.time() - s.derniere_reponse if s.derniere_reponse else 0)
                        journal.ignore("pas_nomme", len(brut), premier_mot=proche, signaux=s.signaux if garder else None)
                        mesures.ignore("pas_nomme")
                        await s.send({"type": "ignored", "reason": "pas_nomme"}); continue
                    # L'énoncé était pour Bulle, mais Whisper n'y croit pas lui-même : mieux vaut faire
                    # répéter que d'agir. Le contrôle vient APRÈS « est-ce pour moi ? » — parler pour une
                    # conversation du salon qui ne lui était pas adressée serait pire que le défaut corrigé.
                    if douteuse(conf):
                        log.info("STT %.1f s : transcription douteuse (confiance %.2f), %d caractères", dt, conf, len(brut))
                        journal.ignore("transcription_douteuse", len(brut), eveille=int(awake),
                                       signaux=s.signaux if _signaux_a_garder("transcription_douteuse", None, 0) else None)
                        mesures.ignore("transcription_douteuse")
                        await s.send({"type": "ignored", "reason": "transcription_douteuse"})
                        await s.say(regles.c("phrase_mal_comprise", "Je n'ai pas bien entendu. Tu peux répéter ?"))
                        continue
                    # La confiance est journalisée avec le texte (qui, lui, a déjà passé « est-ce pour
                    # moi ? ») : c'est de ces lignes-là que sortira le bon seuil, mesuré dans le salon plutôt
                    # que sur de la voix de synthèse. La jauge Prometheus ne garde que la dernière valeur.
                    log.info("STT %.1f s : %r%s", dt, text, "" if conf is None else f" (confiance {conf:.2f})")
                    if named:
                        text = vocatif_fin.sub("", vocatif.sub("", text)).strip()
                        if len(text) < 2:
                            await s.send({"type": "wake"}); continue
                    await s.send({"type": "transcript", "text": text})
                    await s.send({"type": "thinking"})
                    await s.answer(text, brut=brut, source="voix", nomme=named, eveille=awake)
                elif msg.get("text"):
                    d = json.loads(msg["text"])
                    if d.get("type") == "reset": s.history = []; s.dernier_echange = None
                    elif d.get("type") == "audio":
                        s.awake_next = bool(d.get("awake"))
                        s.signaux = _signaux_propres(d.get("signaux"))
                        if "fuite" in s.signaux: mesures.fuite(s.signaux["fuite"])
                    elif d.get("type") == "simulation": s.sim = bool(d.get("actif", True))
                    elif d.get("type") == "text" and d.get("text", "").strip():
                        await s.send({"type": "thinking"})
                        await s.answer(d["text"].strip(), source="banc" if s.sim else "texte")
            except WebSocketDisconnect:
                raise
            except Exception as e:
                log.exception("erreur")
                await s.send({"type": "error", "message": str(e)})
    except (WebSocketDisconnect, RuntimeError):
        pass
    clients.discard(id(ws)); mesures.clients(len(clients))
    log.info("client déconnecté")


try:
    import suivi                                   # page de suivi (/suivi) — facultative
    suivi.monter(app)
except ImportError:
    pass
