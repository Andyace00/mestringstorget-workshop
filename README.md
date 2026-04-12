# Mestringstorget Workshop Server

Live digital input-server for ledergruppen Helse og mestring i Nordre Follo kommune.

## Endepunkter
- `/admin` — fasilitator-kontrollpanel
- `/wall` — live wall til storskjerm
- `/p` — deltaker-input

## Lokal kjøring
```
pip install -r requirements.txt
python workshop_server.py
```

## Deploy til Render
1. Push til GitHub
2. New → Web Service → koble GitHub-repo
3. Render finner `render.yaml` automatisk
4. Free tier holder for én workshop
