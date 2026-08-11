#!/usr/bin/env python3
"""
Costruisce prezzi.json: il listino pubblico letto dalla dashboard.

Principi:
- nessuna dipendenza esterna (solo libreria standard): niente da installare,
  il workflow parte e basta;
- se una fonte non risponde NON si sovrascrive il buono con il vuoto: il file
  precedente sopravvive sempre, e lo stato lo dice;
- gli ISIN senza ticker noto vengono risolti da soli e il risultato viene
  memorizzato in config/strumenti.json: la lista impara e non va curata a mano;
- chi non risponde per troppe corse di fila viene dismesso, non cancellato.
"""

import json, os, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

QUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(QUI, "config", "strumenti.json")
DISMESSI = os.path.join(QUI, "config", "dismessi.json")
USCITA = os.path.join(QUI, "prezzi.json")

# Oltre questa soglia di corse fallite di fila lo strumento esce dal listino.
# 10 corse ~ una settimana di borsa con due esecuzioni al giorno.
MAX_ERRORI = 10

UA = "Mozilla/5.0 (compatible; listino-prezzi/1.0; +https://github.com/git-alez/listino-prezzi)"


def http_json(url, tentativi=3):
    """GET che restituisce JSON. Riprova: un errore di rete non deve buttare giù la corsa."""
    ultimo = None
    for n in range(tentativi):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                       # noqa: BLE001 - qui vogliamo davvero ogni errore
            ultimo = e
            time.sleep(1.5 * (n + 1))
    raise RuntimeError(f"{type(ultimo).__name__}: {ultimo}")


def carica(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def salva(path, dati):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)                            # scrittura atomica: mai un file mezzo scritto


# ---------------------------------------------------------------- fonte: Yahoo
def cerca_ticker(isin):
    """ISIN -> simbolo Yahoo. Preferisce Milano (.MI), poi qualunque piazza europea.

    È il pezzo che rende la lista autosufficiente: chi aggiunge un ISIN non deve
    sapere come Yahoo lo chiami."""
    url = "https://query2.finance.yahoo.com/v1/finance/search?q=" + urllib.parse.quote(isin)
    d = http_json(url)
    quotes = d.get("quotes") or []
    if not quotes:
        return None
    def punteggio(q):
        s = q.get("symbol", "")
        if s.endswith(".MI"):  return 0             # borsa italiana: la preferita
        if s.endswith((".DE", ".PA", ".AS", ".L")): return 1
        return 2
    quotes.sort(key=punteggio)
    return quotes[0].get("symbol")


def prezzo_di(ticker):
    """Ultimo prezzo noto + data. Restituisce (prezzo, 'YYYY-MM-DD', valuta)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(ticker) + "?range=5d&interval=1d")
    d = http_json(url)
    res = ((d.get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise RuntimeError("risposta senza dati")
    meta = res.get("meta") or {}
    p = meta.get("regularMarketPrice")
    ts = meta.get("regularMarketTime")
    valuta = meta.get("currency") or "EUR"
    if p is None:                                    # ripiego sull'ultima chiusura disponibile
        chiusure = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        chiusure = [c for c in chiusure if c is not None]
        if not chiusure:
            raise RuntimeError("nessun prezzo nella risposta")
        p = chiusure[-1]
    data = (datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
            if ts else datetime.now(timezone.utc).date().isoformat())
    return float(p), data, valuta


def plausibile(p, tipo):
    """Guardia grossolana contro i valori assurdi: meglio nessun prezzo che uno sbagliato."""
    if p is None or p <= 0:
        return False
    if tipo == "bond":                               # quotazione in percentuale del nominale
        return 1 <= p <= 300
    return 0.0001 <= p <= 1_000_000


# ---------------------------------------------------------------------- corsa
def main():
    strumenti = carica(CONFIG, [])
    dismessi = carica(DISMESSI, [])
    vecchio = carica(USCITA, {"prezzi": {}})
    prezzi = dict(vecchio.get("prezzi") or {})       # si parte dal buono precedente

    if not strumenti:
        print("config/strumenti.json è vuoto: niente da fare.")
        return 0

    # Prezzi orfani: chi è stato tolto a mano dalla lista non deve restare pubblicato
    # per sempre solo perché il file precedente lo conteneva.
    attivi = {s.get("isin") for s in strumenti if s.get("isin")}
    for isin in [k for k in prezzi if k not in attivi]:
        del prezzi[isin]
        print(f"  rimosso dal listino (non più in lista): {isin}")

    ok = err = nuovi_ticker = 0
    tenuti = []
    for n_str, s in enumerate(strumenti):
        # Una pausa breve tra gli strumenti: qualche decina di richieste ravvicinate
        # è il modo più veloce per farsi rifiutare dalla fonte.
        if n_str:
            time.sleep(0.7)
        isin = s.get("isin")
        if not isin:
            continue
        tipo = s.get("tipo") or "azione"

        # 1. ticker mancante: lo si cerca una volta e resta memorizzato
        if not s.get("ticker"):
            try:
                t = cerca_ticker(isin)
                if t:
                    s["ticker"] = t
                    nuovi_ticker += 1
                    print(f"  ticker trovato  {isin} -> {t}")
                else:
                    print(f"  ticker assente  {isin}: nessun simbolo su Yahoo")
            except Exception as e:                   # noqa: BLE001
                print(f"  ricerca fallita {isin}: {e}")

        # 2. prezzo
        if s.get("ticker"):
            try:
                p, data, valuta = prezzo_di(s["ticker"])
                if plausibile(p, tipo):
                    prezzi[isin] = {"p": round(p, 4), "d": data, "src": "yahoo", "ccy": valuta}
                    s["errori_consecutivi"] = 0
                    ok += 1
                    print(f"  ok  {isin:14} {s['ticker']:12} {p:>12.4f} {valuta} ({data})")
                else:
                    raise RuntimeError(f"valore non plausibile: {p}")
            except Exception as e:                   # noqa: BLE001
                s["errori_consecutivi"] = int(s.get("errori_consecutivi") or 0) + 1
                err += 1
                print(f"  ERR {isin:14} {s.get('ticker','—'):12} {e}  "
                      f"(fallimenti di fila: {s['errori_consecutivi']})")
        else:
            s["errori_consecutivi"] = int(s.get("errori_consecutivi") or 0) + 1
            err += 1

        # 3. dismissione: fuori dal listino, ma conservato con la sua storia
        if int(s.get("errori_consecutivi") or 0) >= MAX_ERRORI:
            s["dismesso_il"] = datetime.now(timezone.utc).date().isoformat()
            s["motivo"] = f"{MAX_ERRORI} corse consecutive senza prezzo"
            dismessi.append(s)
            prezzi.pop(isin, None)
            print(f"  DISMESSO {isin}: {s['motivo']}")
        else:
            tenuti.append(s)

    # Validazione d'insieme: se è andato storto quasi tutto è più probabile un
    # guasto della fonte che un delisting di massa. In quel caso non si pubblica.
    if ok == 0 and err > 0:
        print(f"\nNESSUN prezzo ottenuto su {err} tentativi: fonte probabilmente giù. "
              f"prezzi.json NON viene toccato.")
        salva(CONFIG, tenuti)                        # i contatori sì, quelli servono
        salva(DISMESSI, dismessi)
        return 1

    uscita = {
        "aggiornato_al": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonti": {"yahoo": {"stato": "ok" if ok else "errore", "ok": ok, "errori": err}},
        "prezzi": prezzi,
    }
    salva(USCITA, uscita)
    salva(CONFIG, tenuti)
    salva(DISMESSI, dismessi)
    print(f"\nFatto: {ok} prezzi aggiornati, {err} falliti, {nuovi_ticker} ticker imparati, "
          f"{len(prezzi)} strumenti nel listino.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
