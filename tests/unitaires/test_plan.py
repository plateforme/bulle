"""Les plans affichés pour un lieu (cerveau/plan.py) — géométrie et choix d'échelle, sans réseau.

Rien ici ne télécharge quoi que ce soit : on teste la projection, la clé de cache, la distance dite à voix haute
et la reconnaissance des catégories. Le dessin lui-même (tuiles, Overpass) n'est pas du ressort de ces tests.
"""
import plan


# ---------------------------------------------------------------- projection Web Mercator
def test_projection_et_inverse_se_referment():
    lat, lon, zoom = 45.5088, -73.5678, 15          # Place des Arts, Montréal
    x, y = plan._xy(lat, lon, zoom)
    retour_lat, retour_lon = plan._latlon(x, y, zoom)
    assert abs(retour_lat - lat) < 1e-9
    assert abs(retour_lon - lon) < 1e-9


def test_le_meridien_et_l_equateur_tombent_au_centre():
    x, y = plan._xy(0, 0, 1)
    assert abs(x - 1) < 1e-9 and abs(y - 1) < 1e-9   # 2×2 tuiles au zoom 1 : le centre est en (1, 1)


def test_aller_a_l_est_augmente_x_et_aller_au_nord_diminue_y():
    """Le y des tuiles descend vers le sud : une erreur de signe ici cadrerait le plan à l'envers."""
    x0, y0 = plan._xy(45.5, -73.6, 15)
    x_est, _ = plan._xy(45.5, -73.4, 15)
    _, y_nord = plan._xy(45.7, -73.6, 15)
    assert x_est > x0
    assert y_nord < y0


# ---------------------------------------------------------------- clé de cache
def test_la_cle_est_stable_pour_les_memes_coordonnees():
    """Le cerveau donne au Pi l'adresse du plan AVANT de l'avoir dessiné : la clé doit être déterministe."""
    a = plan.cle_de(45.5088, -73.5678, 360, 280, 15)
    b = plan.cle_de(45.5088, -73.5678, 360, 280, 15)
    assert a == b
    assert len(a) == 16 and all(c in "0123456789abcdef" for c in a)


def test_la_cle_change_avec_le_zoom_et_le_cadre():
    base = plan.cle_de(45.5088, -73.5678, 360, 280, 15)
    assert plan.cle_de(45.5088, -73.5678, 360, 280, 14) != base
    assert plan.cle_de(45.5088, -73.5678, 480, 280, 15) != base
    assert plan.cle_de(45.5100, -73.5678, 360, 280, 15) != base


# ---------------------------------------------------------------- distance et direction (dites à voix haute)
def test_plein_nord_et_plein_est():
    km, direction = plan.distance_direction((45.0, -73.0), (46.0, -73.0))
    assert direction == "nord"
    assert 110 < km < 112

    km, direction = plan.distance_direction((45.0, -73.0), (45.0, -72.0))
    assert direction == "est"
    assert 78 < km < 80


def test_les_diagonales_sont_nommees():
    assert plan.distance_direction((45.0, -73.0), (45.5, -72.5))[1] == "nord-est"
    assert plan.distance_direction((45.0, -73.0), (44.5, -73.5))[1] == "sud-ouest"


def test_un_point_sur_lui_meme_est_a_zero():
    km, _ = plan.distance_direction((45.5088, -73.5678), (45.5088, -73.5678))
    assert km == 0


# ---------------------------------------------------------------- échelle
def test_plus_c_est_loin_plus_on_dezoome():
    """À 3,5 m de la TV, un plan de rue pour un lieu à 40 km ne montre plus rien d'utile."""
    assert plan.zoom_utile(0.4) == 16
    assert plan.zoom_utile(2) == 15
    assert plan.zoom_utile(5) == 14
    assert plan.zoom_utile(20) == 12
    assert plan.zoom_utile(100) == 10
    assert plan.zoom_utile(500) == 8
    assert plan.zoom_utile(5000) == 6


def test_le_zoom_ne_remonte_jamais_avec_la_distance():
    precedent = 99
    for km in (0.1, 1, 3, 8, 25, 120, 600, 3000):
        z = plan.zoom_utile(km)
        assert z <= precedent
        precedent = z


# ---------------------------------------------------------------- catégories de lieux
def test_une_categorie_devient_un_tag_osm():
    """Nominatim cherche des NOMS : « une pharmacie » lui renvoyait un commerce nommé « Une », à Valence."""
    assert plan.categorie("trouve-moi une pharmacie") == "amenity=pharmacy"
    assert plan.categorie("où est la SAQ la plus proche") == "shop=alcohol"
    assert plan.categorie("un dépanneur") == "shop=convenience"


def test_la_categorie_la_plus_longue_gagne():
    """« station service » et « essence » mènent au même tag, mais « service » seul ne doit rien déclencher."""
    assert plan.categorie("la station service la plus proche") == "amenity=fuel"
    assert plan.categorie("un bureau de poste") == "amenity=post_office"


def test_un_nom_propre_ne_correspond_a_aucune_categorie():
    """Il part alors à Nominatim, qui est fait pour ça."""
    assert plan.categorie("Où est la Place des Arts ?") is None
    assert plan.categorie("le Stade olympique") is None


def test_les_accents_ne_gênent_pas_la_reconnaissance():
    assert plan.categorie("une épicerie") == "shop=supermarket"
    assert plan.categorie("un hôpital") == "amenity=hospital"
    assert plan._sans_accents("Bibliothèque") == "bibliotheque"
