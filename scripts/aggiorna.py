#!/usr/bin/env python3
"""
Costruisce prezzi.json: il listino pubblico letto dalla dashboard.

Due fonti, perché nessuna copre tutto:
- obbligazioni: l'export CSV di fine giornata di simpletoolsforinvestors.eu, che
  contiene i titoli quotati su Borsa Italiana con prezzo, rendimento e duration;
- azioni, ETF ed ETC: l'endpoint pubblico di Yahoo Finance.

Principi:
- nessuna dipendenza esterna (solo libreria standard): niente da installare;
- se una fonte non risponde NON si sovrascrive il buono con il vuoto: il file
  precedente sopravvive sempre, e lo stato lo dice;
- gli ISIN senza ticker noto vengono risolti da soli e il risultato viene
  memorizzato in config/strumenti.json: la lista impara e non va curata a mano;
- chi non risponde per troppe corse di fila viene dismesso, non cancellato.
"""

import json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

QUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(QUI, "config", "strumenti.json")
DISMESSI = os.path.join(QUI, "config", "dismessi.json")
USCITA = os.path.join(QUI, "prezzi.json")

# Oltre questa soglia di corse fallite di fila lo strumento esce dal listino.
# 10 corse ~ una settimana di borsa con due esecuzioni al giorno.
MAX_ERRORI = 10

# Pagina indice dell'export: i link contengono un hash che cambia, quindi non si
# può cablarli. Si legge la pagina e si trova il link della riga giusta.
STFI_INDICE = "https://www.simpletoolsforinvestors.eu/documentivari.php"
STFI_RIGA = "Rendimenti e durate calcolati End of Day"
# Rete di sicurezza: indirizzo verificato funzionante l'11/08/2026. Se il
# riconoscimento del link nella pagina fallisce si prova questo, e se risponde con
# un CSV valido la corsa va avanti lo stesso. Quando l'hash cambierà questo darà
# 404 e resterà solo la strada normale, che è il motivo per cui non ci si affida
# soltanto a lui.
STFI_NOTO = "https://www.simpletoolsforinvestors.eu/data/export/99BD23A2F237F8386C1D70B17F5C9ABA.csv"

UA = "Mozilla/5.0 (compatible; listino-prezzi/1.0; +https://github.com/git-alez/listino-prezzi)"


def http(url, tentativi=3, testo=True):
    """GET con qualche tentativo: un errore di rete non deve buttare giù la corsa."""
    ultimo = None
    for n in range(tentativi):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                dati = r.read()
                return dati.decode("utf-8", "replace") if testo else dati
        except Exception as e:                       # noqa: BLE001
            ultimo = e
            time.sleep(1.5 * (n + 1))
    raise RuntimeError(f"{type(ultimo).__name__}: {ultimo}")


def http_json(url, tentativi=3):
    return json.loads(http(url, tentativi))


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
    os.replace(tmp, path)                            # scrittura atomica


def num(s):
    """I numeri dell'export sono all'italiana: virgola decimale, campo vuoto ammesso."""
    s = (s or "").strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ------------------------------------------------------- fonte 1: obbligazioni
def bond_da_stfi():
    """Tutti i bond dell'export EOD: {ISIN: {p, d, ytm, dur, ccy}}.

    Si pubblicano TUTTI, non solo quelli in lista: sono dati di listino pubblico,
    coprono qualunque obbligazione tu aggiunga senza doverla registrare da nessuna
    parte, e in mezzo a migliaia di titoli i tuoi non sono riconoscibili."""
    pagina = http(STFI_INDICE)
    # Niente ipotesi sulla forma dell'HTML: si spezza la pagina nelle righe della
    # tabella, in ognuna si tolgono i tag per leggere il testo, e nella riga giusta
    # si prende il primo collegamento a un .csv, relativo o assoluto che sia.
    # (La versione precedente pretendeva un indirizzo che iniziasse con /data/export
    #  e non trovava nulla: era stata scritta guardando la pagina già convertita in
    #  testo, non l'HTML vero.)
    riga = None
    for tr in re.split(r'<tr\b', pagina, flags=re.I):
        testo = re.sub(r'<[^>]+>', ' ', tr)
        if STFI_RIGA.lower() in testo.lower():
            m = re.search(r'href=["\']([^"\']+\.csv)["\']', tr, re.I)
            if m:
                riga = m.group(1)
                break
    if not riga:                                     # ripiego: primo .csv dell'export
        m = re.search(r'href=["\']([^"\']*export[^"\']*\.csv)["\']', pagina, re.I)
        riga = m.group(1) if m else None
    if not riga:
        tutti = re.findall(r'href=["\']([^"\']+\.csv)["\']', pagina, re.I)
        print(f"  link non riconosciuto nella pagina ({len(pagina)} caratteri, "
              f"{len(tutti)} link .csv: {tutti[:3]}) — provo l'indirizzo noto")
    url = urllib.parse.urljoin(STFI_INDICE, riga) if riga else STFI_NOTO
    print(f"  export EOD: {url}")

    testo = http(url)
    righe = testo.splitlines()
    intest = [c.strip().lower() for c in righe[0].split(";")]
    idx = {c: i for i, c in enumerate(intest)}
    for atteso in ("isincode", "price", "referencedate"):
        if atteso not in idx:
            raise RuntimeError(f"colonna '{atteso}' assente: formato cambiato")

    out = {}
    for r in righe[1:]:
        c = r.split(";")
        if len(c) < len(intest) - 1:
            continue
        isin = c[idx["isincode"]].strip()
        p = num(c[idx["price"]])
        if not isin or p is None or not (1 <= p <= 300):
            continue
        d = c[idx["referencedate"]].strip()           # gg/mm/aaaa -> aaaa-mm-gg
        if "/" in d:
            g, m_, a = d.split("/")
            d = f"{a}-{m_}-{g}"
        rec = {"p": round(p, 4), "d": d, "src": "stfi",
               "ccy": (c[idx["currencycode"]].strip() if "currencycode" in idx else "EUR") or "EUR"}
        # Rendimento e duration già calcolati: la dashboard li usa in Analisi obbligazioni
        if "grossytm" in idx:
            v = num(c[idx["grossytm"]])
            if v is not None:
                rec["ytm"] = v
        if "grossduration" in idx:
            v = num(c[idx["grossduration"]])
            if v is not None:
                rec["dur"] = v
        out[isin] = rec
    return out


# --------------------------------------------- fonte 2: azioni, ETF, ETC (Yahoo)
def cerca_ticker(isin):
    """ISIN -> simbolo Yahoo, preferendo Milano e poi le altre piazze europee."""
    d = http_json("https://query2.finance.yahoo.com/v1/finance/search?q=" + urllib.parse.quote(isin))
    quotes = d.get("quotes") or []
    if not quotes:
        return None
    def punteggio(q):
        s = q.get("symbol", "")
        if s.endswith(".MI"):  return 0
        if s.endswith((".DE", ".PA", ".AS", ".L", ".SG")): return 1
        return 2
    quotes.sort(key=punteggio)
    return quotes[0].get("symbol")


def prezzo_yahoo(ticker):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(ticker) + "?range=5d&interval=1d")
    d = http_json(url)
    res = ((d.get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise RuntimeError("risposta senza dati")
    meta = res.get("meta") or {}
    p, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    valuta = meta.get("currency") or "EUR"
    if p is None:
        chiusure = [x for x in (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or [] if x is not None]
        if not chiusure:
            raise RuntimeError("nessun prezzo nella risposta")
        p = chiusure[-1]
    data = (datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
            if ts else datetime.now(timezone.utc).date().isoformat())
    return float(p), data, valuta


def plausibile(p, tipo):
    if p is None or p <= 0:
        return False
    if tipo == "bond":
        return 1 <= p <= 300
    return 0.0001 <= p <= 1_000_000


# ---------------------------------------------------------------------- corsa
def main():
    strumenti = carica(CONFIG, [])
    dismessi = carica(DISMESSI, [])
    vecchio = carica(USCITA, {"prezzi": {}})
    prezzi = dict(vecchio.get("prezzi") or {})
    stato = {}

    # --- obbligazioni: una sola richiesta per tutte -------------------------
    print("Obbligazioni (export EOD simpletoolsforinvestors):")
    try:
        bond = bond_da_stfi()
        prezzi.update(bond)
        stato["bond"] = {"stato": "ok", "titoli": len(bond)}
        print(f"  {len(bond)} obbligazioni pubblicate")
    except Exception as e:                           # noqa: BLE001
        stato["bond"] = {"stato": "errore", "msg": str(e)}
        print(f"  ERRORE: {e} — restano i prezzi della corsa precedente")

    # --- il resto, uno per uno --------------------------------------------
    print("\nAzioni, ETF ed ETC (Yahoo):")
    ok = err = nuovi = 0
    tenuti = []
    # I contatori si alzano dentro il ciclo, ma se il guasto sia del singolo titolo
    # o della fonte lo si sa solo alla fine: si tiene com'erano per poterli rimettere.
    contatori_iniziali = {s.get("isin"): int(s.get("errori_consecutivi") or 0)
                          for s in strumenti}
    for n_str, s in enumerate(strumenti):
        isin = s.get("isin")
        if not isin:
            continue
        tipo = s.get("tipo") or "azione"

        # I bond arrivano dall'export: se sono già lì non si interroga Yahoo, che
        # per le obbligazioni europee non ha nulla e li farebbe dismettere a torto.
        if tipo == "bond":
            if isin in prezzi:
                s["errori_consecutivi"] = 0
                print(f"  ok  {isin:14} (export)      {prezzi[isin]['p']:>10}")
            else:
                s["errori_consecutivi"] = int(s.get("errori_consecutivi") or 0) + 1
                print(f"  --  {isin:14} non presente nell'export "
                      f"(fallimenti di fila: {s['errori_consecutivi']})")
            tenuti.append(s)
            continue

        if n_str:
            time.sleep(0.7)                          # non martellare la fonte

        if not s.get("ticker"):
            try:
                t = cerca_ticker(isin)
                if t:
                    s["ticker"] = t
                    nuovi += 1
                    print(f"  ticker trovato  {isin} -> {t}")
                else:
                    print(f"  ticker assente  {isin}")
            except Exception as e:                   # noqa: BLE001
                print(f"  ricerca fallita {isin}: {e}")

        if s.get("ticker"):
            try:
                p, data, valuta = prezzo_yahoo(s["ticker"])
                if plausibile(p, tipo):
                    prezzi[isin] = {"p": round(p, 4), "d": data, "src": "yahoo", "ccy": valuta}
                    s["errori_consecutivi"] = 0
                    ok += 1
                    print(f"  ok  {isin:14} {s['ticker']:14} {p:>12.4f} {valuta} ({data})")
                else:
                    raise RuntimeError(f"valore non plausibile: {p}")
            except Exception as e:                   # noqa: BLE001
                s["errori_consecutivi"] = int(s.get("errori_consecutivi") or 0) + 1
                err += 1
                print(f"  ERR {isin:14} {s.get('ticker','—'):14} {e}  "
                      f"(fallimenti di fila: {s['errori_consecutivi']})")
        else:
            s["errori_consecutivi"] = int(s.get("errori_consecutivi") or 0) + 1
            err += 1

        # La dismissione si decide dopo il ciclo: qui non si sa ancora se ha
        # fallito il titolo o la fonte.
        tenuti.append(s)

    stato["yahoo"] = {"stato": "ok" if ok else "errore", "ok": ok, "errori": err}

    # Una fonte giu' non e' colpa dei titoli che la usano. Senza questa distinzione
    # cinque giorni di Yahoo irraggiungibile dismetterebbero l'intera lista degli
    # ETF: provato il 19/08/2026, dieci strumenti su sedici in una sola corsa.
    # La soglia dei dieci fallimenti serve a togliere chi ha smesso di quotare per
    # conto suo, non chi non risponde perche' nessuno risponde.
    bond_giu = stato["bond"]["stato"] != "ok"
    yahoo_giu = ok == 0 and err > 0
    if bond_giu or yahoo_giu:
        quali = ", ".join(q for q, giu in (("obbligazioni", bond_giu), ("Yahoo", yahoo_giu)) if giu)
        print(f"\nFonte non disponibile ({quali}): i contatori dei titoli che ne "
              f"dipendono tornano come prima, nessuna dismissione.")
        for s in tenuti:
            e_bond = s.get("tipo") == "bond"
            if (e_bond and bond_giu) or (not e_bond and yahoo_giu):
                s["errori_consecutivi"] = contatori_iniziali.get(s.get("isin"), 0)

    for s in list(tenuti):
        if int(s.get("errori_consecutivi") or 0) >= MAX_ERRORI:
            s["dismesso_il"] = datetime.now(timezone.utc).date().isoformat()
            s["motivo"] = f"{MAX_ERRORI} corse consecutive senza prezzo"
            dismessi.append(s)
            tenuti.remove(s)
            prezzi.pop(s.get("isin"), None)
            print(f"  DISMESSO {s.get('isin')}: {s['motivo']}")

    # Se è fallito tutto è più probabile un guasto delle fonti che un delisting
    # di massa: in quel caso non si pubblica e il file buono resta dov'è.
    if ok == 0 and stato["bond"]["stato"] != "ok":
        print("\nNessuna fonte ha risposto: prezzi.json NON viene toccato.")
        salva(CONFIG, tenuti)
        salva(DISMESSI, dismessi)
        return 1

    salva(USCITA, {
        "aggiornato_al": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonti": stato,
        "prezzi": prezzi,
    })
    salva(CONFIG, tenuti)
    salva(DISMESSI, dismessi)
    kb = os.path.getsize(USCITA) // 1024
    print(f"\nFatto: {len(prezzi)} strumenti nel listino ({kb} KB), "
          f"{ok} da Yahoo, {err} falliti, {nuovi} ticker imparati.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
