#!/usr/bin/env python3
"""
Fa crescere il listino da solo, con quello che le dashboard chiedono davvero.

Gira PRIMA di aggiorna.py: legge dal worker gli strumenti che qualcuno ha cercato e
che il listino non copriva, e promuove in config/strumenti.json quelli che superano
i filtri. Da lì in poi ci pensa aggiorna.py: lo strumento entra nel listino e il
worker non serve più per quello.

Perché i filtri (senza, basterebbe un codice digitato male per sporcare il listino):
- richiesto in almeno DUE giorni distinti: un errore di battitura si fa una volta;
- deve avere un prezzo ADESSO: se non quota da nessuna parte non entra, e resta
  parcheggiato nel registro finché non matura;
- massimo 20 promozioni per corsa: un tetto contro il flooding.

Nessuna dipendenza esterna, come aggiorna.py: solo libreria standard.
Se il worker non risponde lo script esce in silenzio con successo — l'aggiornamento
dei prezzi non deve fermarsi perché il sensore è giù.
"""

import json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

# Stessa forma riconosciuta dal worker: due lettere di paese, nove alfanumerici, una
# cifra di controllo. Serve un controllo stretto perché tutto ciò che non è un ISIN
# viene preso per un ticker, e un codice storpiato entrerebbe nel listino come tale.
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

QUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(QUI, "config", "strumenti.json")
DISMESSI = os.path.join(QUI, "config", "dismessi.json")

MAX_PROMOZIONI = 20
GIORNI_MINIMI = 2

UA = "Mozilla/5.0 (compatible; listino-prezzi/2.0; +https://github.com/git-alez/listino-prezzi)"
WORKER = (os.environ.get("WORKER_URL") or "").rstrip("/")
TOKEN = os.environ.get("WORKER_TOKEN") or ""


def http(url, dati=None, tentativi=3):
    ultimo = None
    for n in range(tentativi):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(dati).encode() if dati is not None else None,
                headers={"User-Agent": UA, "Content-Type": "application/json"},
                method="POST" if dati is not None else "GET",
            )
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                       # noqa: BLE001
            ultimo = e
            time.sleep(1.5 * (n + 1))
    raise RuntimeError(f"{type(ultimo).__name__}: {ultimo}")


def leggi(percorso, vuoto):
    try:
        with open(percorso, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                # noqa: BLE001
        return vuoto


def salva(percorso, dati):
    """Scrittura atomica: un'interruzione a metà non deve lasciare un file monco."""
    tmp = percorso + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, percorso)


def prezzo_yahoo(simbolo):
    d = http("https://query1.finance.yahoo.com/v8/finance/chart/"
             + urllib.parse.quote(simbolo) + "?range=5d&interval=1d")
    res = ((d.get("chart") or {}).get("result") or [None])[0]
    if not res:
        return None, None
    meta = res.get("meta") or {}
    p = meta.get("regularMarketPrice")
    if p is None:
        chiusure = [x for x in (((res.get("indicators") or {}).get("quote")
                                 or [{}])[0]).get("close") or [] if x is not None]
        p = chiusure[-1] if chiusure else None
    return p, (meta.get("currency") or "EUR")


def cerca_simbolo(chiave):
    """Stessa preferenza di aggiorna.py: prima Milano, poi le altre piazze europee."""
    if not ISIN.match(chiave):
        # Non è un ISIN: o è un ticker scritto a mano nella dashboard, o è un codice
        # storpiato. Si accetta come ticker solo se ha la forma di un ticker; la
        # prova del prezzo, subito dopo, scarta comunque quelli inventati.
        return chiave if re.match(r"^[A-Z0-9]{1,6}(\.[A-Z]{1,3})?$", chiave) else None
    d = http("https://query2.finance.yahoo.com/v1/finance/search?q="
             + urllib.parse.quote(chiave))
    q = [x for x in (d.get("quotes") or []) if x.get("symbol")]
    if not q:
        return None
    def punteggio(x):
        s = x["symbol"]
        if s.endswith(".MI"):
            return 0
        if s.endswith((".DE", ".PA", ".AS", ".L", ".SW")):
            return 1
        return 2
    q.sort(key=punteggio)
    return q[0]["symbol"]


def main():
    if not WORKER or not TOKEN:
        print("WORKER_URL o WORKER_TOKEN non impostati: nessuna autoespansione.")
        return 0

    try:
        pendenti = http(f"{WORKER}/pending?token={urllib.parse.quote(TOKEN)}")
    except Exception as e:                           # noqa: BLE001
        # Il sensore giù non deve fermare i prezzi: si riprova alla corsa dopo.
        print(f"Registro non raggiungibile ({e}): salto l'autoespansione.")
        return 0

    if not isinstance(pendenti, list) or not pendenti:
        print("Nessuno strumento in attesa.")
        return 0

    strumenti = leggi(CONFIG, [])
    dismessi = leggi(DISMESSI, [])
    noti = {s.get("isin") for s in strumenti} | {s.get("isin") for s in dismessi}
    oggi = datetime.now(timezone.utc).date().isoformat()

    print(f"{len(pendenti)} strumenti nel registro.")
    promossi, trattati = [], []

    for v in sorted(pendenti, key=lambda x: -(x.get("conta") or 0)):
        isin = (v.get("isin") or "").strip()
        if not isin:
            continue

        if isin in noti:
            # Già in lista (o già dismesso): la richiesta ha fatto il suo lavoro.
            trattati.append(isin)
            print(f"  --  {isin:14} già noto")
            continue

        # Due giorni distinti: le date di prima e ultima richiesta devono differire.
        if v.get("primo") == v.get("ultimo") or (v.get("conta") or 0) < GIORNI_MINIMI:
            print(f"  ..  {isin:14} chiesto una volta sola, aspetta")
            continue

        if len(promossi) >= MAX_PROMOZIONI:
            print(f"  ..  {isin:14} oltre il tetto di {MAX_PROMOZIONI}, alla prossima corsa")
            continue

        try:
            sim = cerca_simbolo(isin)
            time.sleep(0.7)                          # non martellare la fonte
            if not sim:
                raise RuntimeError("nessun simbolo")
            p, ccy = prezzo_yahoo(sim)
            time.sleep(0.7)
            if not p or p <= 0:
                raise RuntimeError("nessun prezzo")
        except Exception as e:                       # noqa: BLE001
            # Non si tratta: resta nel registro e riproverà. Un titolo che non quota
            # oggi può quotare domani, e cancellarlo perderebbe la richiesta.
            print(f"  ERR {isin:14} {e}")
            continue

        strumenti.append({
            "isin": isin, "ticker": sim, "tipo": "etf",
            "nota": "aggiunto da richiesta utente",
            "aggiunto": "auto", "dal": oggi, "errori_consecutivi": 0,
        })
        noti.add(isin)
        promossi.append(isin)
        trattati.append(isin)
        print(f"  OK  {isin:14} {sim:14} {p} {ccy}  → promosso")

    if promossi:
        salva(CONFIG, strumenti)
        print(f"\n{len(promossi)} strumenti promossi: {', '.join(promossi)}")
    else:
        print("\nNessuna promozione in questa corsa.")

    # Si pulisce SOLO ciò che è stato davvero trattato: quello che aspetta ancora
    # deve restare, altrimenti il conteggio dei giorni distinti riparte da zero.
    if trattati:
        try:
            http(f"{WORKER}/pending/pulisci?token={urllib.parse.quote(TOKEN)}",
                 dati={"isin": trattati})
            print(f"Registro ripulito di {len(trattati)} voci.")
        except Exception as e:                       # noqa: BLE001
            # Non è grave: alla prossima corsa si ritrovano e vengono saltati come
            # "già noti". Meglio una voce di troppo che una richiesta persa.
            print(f"Pulizia del registro fallita ({e}): si riproverà.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
