"""Envoi d'un message dans le groupe Signal « BulleAI » (signal-cli-rest-api local, appareil lié au compte de Greg).

  python agent/signal_envoi.py "texte"          envoie le texte
  echo "texte" | python agent/signal_envoi.py -  lit le texte sur l'entrée standard
Relève aussi les messages en attente : un appareil lié qui ne reçoit jamais finit par être délié par Signal.
Configuration (hors dépôt) : ~/.config/bulle/signal.env → SIGNAL_NUMERO, SIGNAL_GROUPE.
"""
import json, os, sys, urllib.request

API = os.environ.get("SIGNAL_API", "http://127.0.0.1:8805")


def conf():
    c = {}
    for l in open(os.path.expanduser("~/.config/bulle/signal.env"), encoding="utf-8"):
        if "=" in l:
            k, v = l.strip().split("=", 1); c[k] = v
    return c


def envoyer(texte):
    c = conf()
    try:   # garder l'appareil lié en vie
        urllib.request.urlopen(f"{API}/v1/receive/{urllib.parse.quote(c['SIGNAL_NUMERO'])}?timeout=5", timeout=60).read()
    except Exception:
        pass
    req = urllib.request.Request(f"{API}/v2/send", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"message": texte, "number": c["SIGNAL_NUMERO"], "recipients": [c["SIGNAL_GROUPE"]]}).encode())
    return json.load(urllib.request.urlopen(req, timeout=120))


if __name__ == "__main__":
    import urllib.parse  # noqa: F401
    texte = sys.stdin.read() if sys.argv[1:] == ["-"] else " ".join(sys.argv[1:])
    if not texte.strip(): sys.exit("rien à envoyer")
    print(envoyer(texte.strip()))
