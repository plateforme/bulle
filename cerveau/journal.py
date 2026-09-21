"""Journal SQLite de Bulle : échanges, retours de Greg, incidents, actions (ce qui a été fait) et tests à faire."""
import json, os, sqlite3, threading, time

CHEMIN = os.environ.get("BULLE_DB", os.path.expanduser("~/kinectface/etat/bulle.db"))
os.makedirs(os.path.dirname(CHEMIN), exist_ok=True)
_lock = threading.Lock()

SCHEMA = """
create table if not exists echanges(
  id integer primary key autoincrement, ts real, source text,           -- voix | texte | banc
  brut text, texte text, nomme integer, eveille integer, ignore_raison text,
  outils text,                                                          -- JSON [{nom, args, resultat, ok, redirige}]
  reponse text, emotion text, duree real, simulation integer default 0);
create table if not exists retours(
  id integer primary key autoincrement, ts real, echange_id integer, phrase text, statut text default 'nouveau');
create table if not exists incidents(
  id integer primary key autoincrement, ts real, nuit text, type text, gravite text, cle text,
  resume text, detail text, statut text default 'ouvert');               -- ouvert | corrige | propose | ignore
create table if not exists actions(
  id integer primary key autoincrement, ts real, auteur text, type text,  -- correction | proposition | deploiement | note
  titre text, detail text, commit_ref text, statut text default 'fait'); -- fait | en_attente | applique | refuse | annule
create table if not exists a_tester(
  id integer primary key autoincrement, ts real, origine text, phrase text, attendu text,
  statut text default 'a_faire', commentaire text);                      -- a_faire | ok | ko
create table if not exists memoire(
  id integer primary key autoincrement, ts real, fait text, source text default 'voix');
create index if not exists i_echanges_ts on echanges(ts);
create index if not exists i_incidents_cle on incidents(cle);
"""

# Colonnes ajoutées après coup : « create table if not exists » ne les pose pas sur une base déjà créée.
AJOUTS = {"echanges": {"premier_mot": "text",      # d'un énoncé ignoré : gardé seulement s'il ressemble au nom
                       "longueur": "integer",      # caractères de la transcription, qu'on ne garde plus
                       "ignore_detail": "text",    # quel motif d'hallucination a filtré
                       "signaux": "text"}}         # JSON : géométrie et niveaux de l'énoncé, JAMAIS de texte


def conn():
    c = sqlite3.connect(CHEMIN, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _migrer(c):
    for table, colonnes in AJOUTS.items():
        existantes = {r[1] for r in c.execute(f"pragma table_info({table})")}
        for nom, typ in colonnes.items():
            if nom not in existantes:
                c.execute(f"alter table {table} add column {nom} {typ}")


with _lock, conn() as _c:
    _c.executescript(SCHEMA)
    _migrer(_c)


def ecrire(sql, params=()):
    with _lock, conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def lire(sql, params=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def echange(**kw):
    kw.setdefault("ts", time.time())
    for cle in ("outils", "signaux"):
        if isinstance(kw.get(cle), (list, dict)):
            kw[cle] = json.dumps(kw[cle], ensure_ascii=False)
    cols = ",".join(kw)
    return ecrire(f"insert into echanges({cols}) values({','.join('?' * len(kw))})", tuple(kw.values()))


def ignore(raison, longueur, premier_mot=None, eveille=0, detail=None, signaux=None):
    """Un énoncé qui n'était pas pour Bulle : on note QU'IL Y EN A EU UN, pas ce qu'il disait.

    L'analyste n'a besoin que de ça — l'heure (pour « ignoré juste après une réponse »), la longueur, et le
    premier mot quand il ressemble au nom (pour « ignoré alors que c'était sûrement pour elle »).

    `signaux` : ce que le Pi a mesuré SANS écouter les mots — durée, niveau, probabilité de parole, retards
    entre micros, angle et distance donnés par la Kinect. Le cerveau ne le passe que pour les énoncés déjà
    signalés comme des ratés de peu (server.py, `_signaux_a_garder`) : d'une conversation du salon qui ne
    s'adressait visiblement pas à Bulle, il ne reste toujours que l'heure, la longueur et la raison.
    """
    return echange(source="voix", nomme=0, eveille=eveille, ignore_raison=raison,
                   longueur=longueur, premier_mot=premier_mot, ignore_detail=detail, signaux=signaux)


# ---------------------------------------------------------------- mémoire longue
# La seule table qui garde de la PAROLE au-delà des durées de vie du journal. Elle n'est donc alimentée que par
# l'outil `retenir`, quand Greg le demande explicitement — jamais par une extraction automatique de ce qui se
# dit au salon. `purger()` ne la nomme pas : un souvenir ne s'efface que quand on demande de l'oublier.
def retenir(fait, source="voix"):
    """Un fait que Greg a demandé de retenir. → identifiant."""
    return ecrire("insert into memoire(ts, fait, source) values(?,?,?)", (time.time(), str(fait).strip(), source))


def souvenirs(limite=None):
    """Les faits retenus, du plus ancien au plus récent. `limite` garde les N PLUS RÉCENTS (et les rend quand
    même dans l'ordre chronologique) : c'est le prompt qu'on plafonne, pas ce que Greg a le droit de retenir."""
    tout_ = lire("select * from memoire order by ts")
    return tout_[-limite:] if limite else tout_


def oublier(ids):
    """→ nombre de souvenirs effacés."""
    ids = [int(i) for i in (ids if isinstance(ids, (list, tuple, set)) else [ids])]
    if not ids: return 0
    with _lock, conn() as c:
        return c.execute(f"delete from memoire where id in ({','.join('?' * len(ids))})", ids).rowcount


def retour(echange_id, phrase):
    return ecrire("insert into retours(ts, echange_id, phrase) values(?,?,?)", (time.time(), echange_id, phrase))


def action(auteur, type_, titre, detail="", commit_ref="", statut="fait"):
    return ecrire("insert into actions(ts, auteur, type, titre, detail, commit_ref, statut) values(?,?,?,?,?,?,?)",
                  (time.time(), auteur, type_, titre, detail, commit_ref, statut))


def a_tester(origine, phrase, attendu):
    return ecrire("insert into a_tester(ts, origine, phrase, attendu) values(?,?,?,?)", (time.time(), origine, phrase, attendu))


# ---------------------------------------------------------------- entretien (appelé par la boucle de nuit)
def effacer_textes_ignores():
    """Efface le texte des énoncés qui n'étaient pas pour Bulle → nombre de lignes nettoyées.

    À faire AVANT la sauvegarde, et c'est tout l'objet de cette fonction séparée : une sauvegarde prise avant
    aurait emporté dans sa copie exactement ce qu'on cherche à faire disparaître (vu le 21/09, à la première
    exécution : 1 071 transcriptions effacées de la base, intactes dans la sauvegarde du jour). Contrairement à
    une durée de vie mal réglée, ce nettoyage n'est jamais quelque chose qu'on voudrait annuler.

    Rejoué à chaque nuit, pas seulement une fois : si une régression du cerveau réécrivait du texte, il ne
    survivrait pas vingt-quatre heures.
    """
    with _lock, conn() as c:
        return c.execute("update echanges set brut = null, texte = null "
                         "where ignore_raison is not null and (brut is not null or texte is not null)").rowcount


def purger(retentions):
    """Applique les durées de vie → {quoi: lignes supprimées}. `retentions` : jours, par clé (voir regles.yaml).

    Les incidents, les actions et les tests à faire portent la mémoire du projet : ils vivent longtemps.
    Les échanges, eux, sont de la conversation — ils s'effacent. À faire APRÈS la sauvegarde : une durée de vie
    mal réglée doit rester réparable.
    """
    j = lambda cle, defaut: time.time() - float(retentions.get(cle, defaut)) * 86400
    bilan = {}
    with _lock, conn() as c:
        for quoi, sql, params in (
                ("échanges", "delete from echanges where ts < ? and ignore_raison is null", (j("echanges_jours", 30),)),
                ("énoncés ignorés", "delete from echanges where ts < ? and ignore_raison is not null", (j("ignores_jours", 7),)),
                ("incidents", "delete from incidents where ts < ?", (j("incidents_jours", 365),)),
                ("actions", "delete from actions where ts < ?", (j("actions_jours", 365),)),
                ("tests faits", "delete from a_tester where ts < ? and statut != 'a_faire'", (j("tests_jours", 90),)),
                ("retours traités", "delete from retours where ts < ? and statut != 'nouveau'", (j("retours_jours", 180),))):
            n = c.execute(sql, params).rowcount
            if n: bilan[quoi] = n
    if bilan:
        with _lock, conn() as c:
            c.execute("vacuum")          # sans ça le fichier ne rend jamais la place ; hors transaction
    return bilan


def sauvegarder(dossier, garder=14):
    """Copie sûre de la base (API backup de SQLite : correcte même pendant une écriture) → chemin du fichier.

    Une copie de fichier pendant que le cerveau écrit donnerait une base corrompue, d'où `Connection.backup`.
    """
    os.makedirs(dossier, exist_ok=True)
    cible = os.path.join(dossier, f"bulle-{time.strftime('%Y-%m-%d')}.db")
    with _lock, conn() as source, sqlite3.connect(cible) as copie:
        source.backup(copie)
    anciennes = sorted(f for f in os.listdir(dossier) if f.startswith("bulle-") and f.endswith(".db"))
    for f in anciennes[:-garder] if garder else []:
        os.remove(os.path.join(dossier, f))
    return cible
