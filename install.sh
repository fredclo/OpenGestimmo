#!/bin/bash
cd "$(dirname "$0")"
echo "=== Installation de l'environnement Python ==="
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
echo "=== Installation terminée ! ==="
echo "Pour lancer le logiciel, exécutez ./run.sh dans le terminal, ou utilisez le script start_silent.sh"
