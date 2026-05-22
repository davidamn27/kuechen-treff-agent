# Kuechen Treff Agent

Python-Webapp fuer die Projekt- und Preispruefung im Kuechenstudio.

## Lokal starten

```bash
python3 server.py
```

Danach ist die App lokal unter `http://127.0.0.1:5173/` erreichbar.

## Online deployen

Die App ist fuer Render vorbereitet. Nach dem Push zu GitHub kann in Render ein neuer Blueprint aus diesem Repository erstellt werden. Render liest `render.yaml` und startet die App mit `python3 server.py`.
