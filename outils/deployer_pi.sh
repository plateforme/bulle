#!/bin/bash
# Déploie la partie Pi (JARVIS) depuis le dépôt de travail du VM : visage, client, suivi, règles, services.
# Ne redémarre que les services dont un fichier a changé. Usage : outils/deployer_pi.sh [--tout]
set -euo pipefail
DEPOT="$(cd "$(dirname "$0")/.." && pwd)"
PI="${BULLE_PI:-plateforme@192.0.2.6}"
SSH="ssh -i $HOME/.ssh/id_bulle_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8"

declare -A FICHIERS=(
  [face.py]=kinectface-face
  [carte.py]=kinectface-face
  [compagnon.py]=kinectface-compagnon
  [pi/tracker.py]=kinectface-tracker
  [config/regles.yaml]="kinectface-compagnon kinectface-tracker"
  # Les unités elles-mêmes : jusqu'au 21/09 elles n'étaient pas déployées, donc le dépôt pouvait promettre un
  # réglage que le Pi n'avait pas — un correctif d'unité relu et fusionné n'atteignait aucune machine.
  [pi/kinectface-face.service]=kinectface-face
  [pi/kinectface-compagnon.service]=kinectface-compagnon
  [pi/kinectface-tracker.service]=kinectface-tracker
  # Geist, les quatre graisses des cartes (refonte du 21/09). carte.py sait retomber sur DejaVu si elles
  # manquent : une police qui n'arrive pas ne doit pas faire disparaître la carte, seulement son allure.
  [pi/polices/Geist-ExtraLight.ttf]=kinectface-face
  [pi/polices/Geist-Light.ttf]=kinectface-face
  [pi/polices/Geist-Regular.ttf]=kinectface-face
  [pi/polices/Geist-Medium.ttf]=kinectface-face
  [pi/polices/OFL.txt]=kinectface-face
)
a_redemarrer=()
recharger=0
# Les polices vivent dans un sous-dossier, seul cas où la destination n'est pas à plat : le créer avant la
# boucle, sinon la redirection « cat > … » échoue sur un dossier absent et le fichier n'arrive jamais.
$SSH "$PI" "mkdir -p kinectface/polices"
for src in "${!FICHIERS[@]}"; do
  dst="kinectface/$(basename "$src")"
  [ "$src" = "config/regles.yaml" ] && dst="kinectface/regles.yaml"
  case "$src" in pi/polices/*) dst="kinectface/polices/$(basename "$src")" ;; esac
  local_sum=$(sha256sum "$DEPOT/$src" | cut -d' ' -f1)
  distant_sum=$($SSH "$PI" "sha256sum $dst 2>/dev/null | cut -d' ' -f1" || true)
  if [ "$local_sum" != "$distant_sum" ] || [ "${1:-}" = "--tout" ]; then
    $SSH "$PI" "cat > $dst.nouveau && mv $dst.nouveau $dst" < "$DEPOT/$src"
    # Relire l'empreinte APRÈS la copie : une redirection d'entrée qui n'aboutit pas ne fait rien échouer, le
    # fichier distant reste l'ancien, et on redémarrerait le service sur l'ancien code en le croyant à jour —
    # « is-active » répondrait « active » (vu le 21/09 en posant les unités à la main).
    verif=$($SSH "$PI" "sha256sum $dst 2>/dev/null | cut -d' ' -f1" || true)
    if [ "$verif" != "$local_sum" ]; then
      echo "ÉCHEC : $src recopié mais l'empreinte distante ne correspond pas (attendu ${local_sum:0:12}, lu ${verif:0:12})"
      exit 1
    fi
    echo "copié : $src"
    # Une unité vit dans /etc/systemd/system ; la copie sous ~/kinectface ne sert qu'à comparer les empreintes.
    case "$src" in *.service)
      $SSH "$PI" "sudo install -m 644 -o root -g root $dst /etc/systemd/system/$(basename "$src")"
      recharger=1 ;;
    esac
    a_redemarrer+=(${FICHIERS[$src]})
  fi
done
# Avant tout redémarrage : sans daemon-reload, systemd relancerait l'ANCIENNE définition, le fichier neuf en place.
if [ "$recharger" = 1 ]; then
  $SSH "$PI" "sudo systemctl daemon-reload"
  echo "systemd rechargé"
fi
if [ ${#a_redemarrer[@]} -gt 0 ]; then
  services=$(printf "%s\n" "${a_redemarrer[@]}" | sort -u | tr '\n' ' ')
  $SSH "$PI" "sudo systemctl restart $services"
  sleep 8
  for s in $services; do
    etat=$($SSH "$PI" "systemctl is-active $s" || true)
    echo "$s : $etat"
    [ "$etat" = "active" ] || { echo "ÉCHEC : $s ne tourne pas"; exit 1; }
  done
else
  echo "rien à déployer"
fi
