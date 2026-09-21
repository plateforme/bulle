"""Le géocodage des lieux (cerveau/plan.geocoder), sans réseau.

Ces tests rejouent de VRAIES réponses de Nominatim, relevées le 21/09 en interrogeant le service à la main.
C'est ce qui leur donne leur valeur : chacun correspond à une panne vécue dans le salon, pas à une hypothèse.

  « Place des Arts à Montréal »  → Nominatim ne renvoyait RIEN (le « à » de la phrase parlée le fait échouer)
  « 3637 University à Montréal » → une succursale de banque à Charlottetown, à 800 km, annoncée sans sourciller
  « la Tour Eiffel »             → doit rester trouvable, elle, bien qu'à 5 500 km

La couture est `plan._nominatim(params)` : on la remplace, et on peut aussi vérifier CE QU'ON A DEMANDÉ.
"""
import plan
import pytest

MAISON = (45.5271, -73.5695)

# --- réponses réelles, réduites aux champs que le code lit
VITRERIE = {"name": "Vitrerie Olympique", "lat": "45.5439", "lon": "-73.5416", "importance": 0,
            "display_name": "Vitrerie Olympique, 4051, Rue Sainte-Catherine Est, Montréal",
            "address": {"house_number": "4051", "road": "Rue Sainte-Catherine Est", "city": "Montréal"}}
BMO_CHARLOTTETOWN = {"name": "BMO", "lat": "46.2382", "lon": "-63.1311", "importance": 0,
                     "display_name": "BMO, 670, University Avenue, Charlottetown, Comté de Queens",
                     "address": {"house_number": "670", "road": "University Avenue", "city": "Charlottetown"}}
TOUR_EIFFEL = {"name": "Tour Eiffel", "lat": "48.8583", "lon": "2.2945", "importance": 0.621,
               "display_name": "Tour Eiffel, 5, Avenue Anatole France, Paris",
               "address": {"road": "Avenue Anatole France", "city": "Paris"}}
ADRESSE_NUE = {"name": "3637", "lat": "45.5062", "lon": "-73.5780", "importance": 0,
               "display_name": "3637, Rue University, Ville-Marie, Montréal",
               "address": {"house_number": "3637", "road": "Rue University", "city": "Montréal"}}
TERRASSE_QUEBEC = {"name": "Terrasse Dufferin", "lat": "46.8087", "lon": "-71.2142", "importance": 0.3,
                "display_name": "Terrasse Dufferin, Vieux-Québec, Québec",
                "address": {"road": "Terrasse Dufferin", "city": "Québec"}}


class FauxService:
    """Répond ce qu'on lui dit, et garde la trace des requêtes envoyées."""

    def __init__(self, *reponses):
        self.reponses, self.demandes = list(reponses), []

    def __call__(self, params):
        self.demandes.append(params)
        return self.reponses.pop(0) if self.reponses else []


@pytest.fixture
def service(monkeypatch):
    def poser(*reponses):
        faux = FauxService(*reponses)
        monkeypatch.setattr(plan, "_nominatim", faux)
        return faux
    return poser


# ---------------------------------------------------------------- la phrase parlée devient une requête
def test_le_a_de_la_phrase_parlee_devient_une_virgule():
    """« Place des Arts à Montréal » ne renvoie rien chez Nominatim ; avec une virgule, il renvoie le bon lieu.

    C'est la cause de « la localisation ne fonctionne plus » (21/09) : à l'oral, on dit toujours « à ».
    """
    assert plan._requete("Place des Arts à Montréal") == "Place des Arts, Montréal"
    assert plan._requete("Vitrerie Olympique a Montreal") == "Vitrerie Olympique, Montreal"


def test_l_article_de_tete_est_retire():
    """« la Tour Eiffel » tombait sur un commerce homonyme à Fès ; « Tour Eiffel » donne celle de Paris."""
    assert plan._requete("la Tour Eiffel") == "Tour Eiffel"
    assert plan._requete("le 3637 University à Montréal") == "3637 University, Montréal"


def test_un_nom_sans_preposition_ne_bouge_pas():
    assert plan._requete("Stade olympique") == "Stade olympique"
    assert plan._requete("Place des Arts") == "Place des Arts"


# ---------------------------------------------------------------- la première passe est bornée à la région
def test_la_premiere_passe_est_bornee(service):
    faux = service([VITRERIE])
    plan.geocoder("Vitrerie Olympique à Montréal", MAISON)
    assert faux.demandes[0]["bounded"] == "1"
    assert "viewbox" in faux.demandes[0]
    assert faux.demandes[0]["q"] == "Vitrerie Olympique, Montréal"


def test_un_lieu_trouve_pres_de_la_maison_arrete_la_recherche(service):
    faux = service([VITRERIE], [BMO_CHARLOTTETOWN])
    trouve = plan.geocoder("Vitrerie Olympique", MAISON)
    assert trouve["nom"] == "Vitrerie Olympique"
    assert len(faux.demandes) == 1          # pas de seconde requête : inutile, et Nominatim est un service public


# ---------------------------------------------------------------- ce qui revient de loin est filtré
def test_un_homonyme_lointain_et_inconnu_est_refuse(service):
    """« 3637 University » a été localisé à Charlottetown (808 km) et annoncé comme un résultat (21/09).

    Mieux vaut dire qu'on ne trouve pas : une adresse de rue n'a aucune raison d'être à 800 km.
    """
    service([], [BMO_CHARLOTTETOWN])        # rien dans la région, puis un homonyme lointain
    assert plan.geocoder("3637 University", MAISON) is None


def test_un_lieu_celebre_lointain_est_garde(service):
    """La contrepartie : la Tour Eiffel est à 5 500 km et doit rester trouvable. Nominatim la dit notoire (0,62)."""
    service([], [TOUR_EIFFEL])
    trouve = plan.geocoder("la Tour Eiffel", MAISON)
    assert trouve["nom"] == "Tour Eiffel"


def test_un_lieu_lointain_dans_la_ville_nommee_est_garde(service):
    """Si Greg dit « à Québec », un résultat à Québec est le bon, même peu notoire et à 250 km."""
    service([], [TERRASSE_QUEBEC])
    trouve = plan.geocoder("Terrasse Dufferin à Québec", MAISON)
    assert trouve["nom"] == "Terrasse Dufferin"


def test_sans_domicile_connu_on_ne_filtre_rien(service):
    """Sans point de référence, aucune distance n'est absurde : on rend ce que Nominatim donne."""
    service([BMO_CHARLOTTETOWN])
    assert plan.geocoder("BMO University Avenue")["nom"] == "BMO"


# ---------------------------------------------------------------- ce que le reste du cerveau lit
def test_le_resultat_porte_de_quoi_juger_de_sa_valeur(service):
    """importance et display_name sont ce sur quoi repose le filtre : s'ils disparaissent, il devient aveugle."""
    service([VITRERIE])
    trouve = plan.geocoder("Vitrerie Olympique", MAISON)
    assert trouve["importance"] == 0
    assert "Sainte-Catherine" in trouve["complet"]
    assert trouve["adresse"] == "4051 Rue Sainte-Catherine Est, Montréal"
    assert (round(trouve["lat"], 4), round(trouve["lon"], 4)) == (45.5439, -73.5416)


# ---------------------------------------------------------------- ce qui s'affiche en grand sur la TV
def test_un_numero_civique_seul_n_est_pas_un_nom(service):
    """Nominatim nomme « 3637 » le point d'une adresse. Affiché en gros sur la TV, ce chiffre ne dit rien :
    Greg a vu « 3637 » en titre, avec la vraie adresse en petit en dessous (21/09)."""
    service([ADRESSE_NUE])
    trouve = plan.geocoder("3637 rue University", MAISON)
    assert trouve["nom"] == "3637 Rue University"
    assert trouve["adresse"] == "3637 Rue University, Montréal"


def test_un_vrai_nom_de_lieu_est_garde_tel_quel(service):
    service([VITRERIE])
    assert plan.geocoder("Vitrerie Olympique", MAISON)["nom"] == "Vitrerie Olympique"


# ---------------------------------------------------------------- catégorie ou nom propre ?
def test_un_nom_propre_qui_contient_un_mot_de_categorie_reste_un_nom():
    """« musée McCord » partait en recherche de musées alentour : le bon musée revenait SANS son adresse, et
    plus rien du tout quand Overpass boudait — la même demande marchait ou pas selon la minute (21/09)."""
    assert plan.categorie("musée McCord à Montréal", "Montréal") is None
    assert plan.categorie("Hôtel du Parlement à Québec", "Montréal") is None
    assert plan.categorie("Café Olimpico", "Montréal") is None


def test_une_demande_generique_reste_une_categorie():
    """La contrepartie : sans nom, c'est bien un commerce qu'on cherche autour de soi."""
    assert plan.categorie("trouve-moi une pharmacie", "Montréal") == "amenity=pharmacy"
    assert plan.categorie("une pharmacie ouverte près d'ici", "Montréal") == "amenity=pharmacy"
    assert plan.categorie("le dépanneur du coin", "Montréal") == "shop=convenience"


def test_un_determinant_indefini_annonce_une_categorie_malgre_un_adjectif():
    """« UN restaurant italien » : « italien » distingue le genre de commerce, pas un établissement."""
    assert plan.categorie("un restaurant italien", "Montréal") == "amenity=restaurant"
    assert plan.categorie("un hôtel pas cher", "Montréal") == "tourism=hotel"


def test_la_ville_de_reference_ne_rend_pas_une_demande_distinctive():
    """Sans ça, « une pharmacie à Montréal » passerait pour le nom d'un lieu."""
    assert plan.categorie("une pharmacie à Montréal", "Montréal") == "amenity=pharmacy"
    assert plan.categorie("pharmacie Montréal", "Montréal") == "amenity=pharmacy"


def test_la_recherche_par_nom_ne_passe_pas_par_overpass(service, monkeypatch):
    """Un nom propre doit aller chez Nominatim : c'est lui qui rend une adresse, Overpass n'en a pas toujours."""
    appels = []
    monkeypatch.setattr(plan, "proche", lambda tag, autour: appels.append(tag))
    service([VITRERIE])
    plan.geocoder("musée McCord à Montréal", MAISON, "Montréal")
    assert appels == []
