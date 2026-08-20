#!/usr/bin/env python3
"""
Fa crescere il listino da solo, con quello che le dashboard chiedono davvero.

Gira PRIMA di aggiorna.py: legge dal worker gli strumenti che qualcuno ha cercato e
che il listino non copriva, e promuove in config/strumenti.json quelli che superano
i filtri. Da l√¨ in poi ci pensa aggiorna.py: lo strumento entra nel listino e il
worker non serve pi√π per quello.

Perch√© i filtri (senza, basterebbe un codice digitato male per sporcare il listino):
- richiesto in almeno DUE giorni distinti: un errore di battitura si fa una volta;
- deve avere un prezzo ADESSO: se non quota da nessuna parte non entra, e resta
  parcheggiato nel registro finch√© non matura;
- massimo 20 promozioni per corsa: un tetto contro il flooding.

Nessuna dipendenza esterna, come aggiorna.py: solo libreria standard.
Se il worker non risponde lo script esce in silenzio con successo ‚Äî l'aggiornamento
dei prezzi non deve fermarsi perch√© il sensore √® gi√π.
"""

import json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

# Stessa forma riconosciuta dal worker: due lettere di paese, nove alfanumerici, una
# cifra di controllo. Serve un controllo stretto perch√© tutto ci√≤ che non √® un ISIN
# viene preso per un ticker, e un codice storpiato entrerebbe nel listino come tale.
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

QUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(QUI, "config", "strumenti.json")
DISMESSI = os.path.join(QUI, "config", "dismessi.json")

MAX_PROMOZIONI = 20
GIORNI_MINIMI = 2
# Oltre questa eta' dalla prima richiesta, un candidato che non e' mai maturato
# (mai un secondo giorno distinto, o mai un prezzo valido) esce dal registro:
# altrimenti un codice sbagliato digitato una volta ci resterebbe per sempre,
# riprovato a ogni corsa senza costrutto.
SCADENZA_GIORNI = 14


def _maschera(isin):
    return isin[:2] + "*" * max(0, len(isin) - 2)


def _senza_token(testo):
    """Il token sta nella query string delle chiamate al worker: se finisse in un
    messaggio d'errore finirebbe anche nei log delle Actions, pubblici quanto il
    repository. Si oscura sempre, senza fidarsi di come ogni libreria formatta le
    proprie eccezioni."""
    t = str(testo)
    if TOKEN:
        t = t.replace(TOKEN, "***").replace(urllib.parse.quote(TOKEN), "***")
    return t


def _eta_giorni(v, oggi):
    try:
        primo = datetime.strptime(v.get("primo") or "", "%Y-%m-%d").date()
    except ValueError:
        return 0
    return (oggi - primo).days

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
    """Scrittura atomica: un'interruzione a met√† non deve lasciare un file monco."""
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


def da_francoforte(isin):
    """Prezzo e natura dello strumento da Borsa di Francoforte, per ISIN diretto.

    Serve a due cose che Yahoo non sa fare: conosce le obbligazioni europee (Yahoo
    no, provati 5 ISIN reali il 20/08/2026, 0 su 5) e dice se il titolo quota in
    percentuale del nominale, cio√® se √® un bond. Senza quest'ultima informazione il
    tipo veniva scritto "etf" a caso, e un bond marchiato etf manda aggiorna.py a
    interrogare Yahoo per un titolo che Yahoo non ha.

    Non solleva mai: se tace si torna a Yahoo, cio√® al comportamento precedente.
    Nota: provata dagli IP di Cloudflare, non ancora da quelli di GitHub Actions ‚Äî
    se qui blocca, il registro della corsa lo dir√† e si resta su Yahoo.
    """
    try:
        d = http("https://api.boerse-frankfurt.de/v1/data/quote_box/single?isin="
                 + urllib.parse.quote(isin))
    except Exception:                                # noqa: BLE001
        return None, None, None
    p = d.get("lastPrice") if isinstance(d, dict) else None
    if not isinstance(p, (int, float)) or p <= 0:
        return None, None, None
    # XFRA quota tutto in euro, titoli esteri compresi: la valuta √® nota per costruzione.
    return p, "EUR", ("bond" if d.get("nominal") is True else None)


def cerca_simbolo(chiave):
    """Stessa preferenza di aggiorna.py: prima Milano, poi le altre piazze europee."""
    if not ISIN.match(chiave):
        # Non √® un ISIN: o √® un ticker scritto a mano nella dashboard, o √® un codice
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
        # Il sensore gi√π non deve fermare i prezzi: si riprova alla corsa dopo.
        print(f"Registro non raggiungibile ({_senza_token(e)}): salto l'autoespansione.")
        return 0

    if not isinstance(pendenti, list) or not pendenti:
        print("Nessuno strumento in attesa.")
        return 0

    strumenti = leggi(CONFIG, [])
    dismessi = leggi(DISMESSI, [])
    noti = {s.get("isin") for s in strumenti} | {s.get("isin") for s in dismessi}
    # Un ticker scritto a mano nel campo "Simbolo prezzo" arriva qui come chiave e NON
    # coincide con l'ISIN della voce che descrive lo stesso strumento: "ENEL.MI" non e'
    # "BIT:ENEL", quindi il controllo su `noti` non lo riconosceva e il 19/08/2026 Enel
    # e' finita in elenco due volte. Si tiene percio' anche l'insieme dei ticker gia'
    # coperti, e si confronta il simbolo RISOLTO prima di promuovere.
    ticker_noti = {(s.get("ticker") or "").upper()
                   for s in strumenti + dismessi if s.get("ticker")}
    oggi_data = datetime.now(timezone.utc).date()
    oggi = oggi_data.isoformat()

    print(f"{len(pendenti)} strumenti nel registro.")
    promossi, trattati = [], []

    for v in sorted(pendenti, key=lambda x: -(x.get("conta") or 0)):
        isin = (v.get("isin") or "").strip()
        if not isin:
            continue

        if isin in noti:
            # Gi√† in lista (o gi√† dismesso): la richiesta ha fatto il suo lavoro.
            trattati.append(isin)
            print(f"  --  {isin:14} gi√† noto")
            continue

        # Due giorni distinti: le date di prima e ultima richiesta devono differire.
        if v.get("primo") == v.get("ultimo") or (v.get("conta") or 0) < GIORNI_MINIMI:
            if _eta_giorni(v, oggi_data) >= SCADENZA_GIORNI:
                trattati.append(isin)
                print(f"  üóë  {_maschera(isin):14} mai un secondo giorno in {SCADENZA_GIORNI}g, scartato")
            else:
                print(f"  ..  {_maschera(isin):14} chiesto una volta sola, aspetta")
            continue

        if len(promossi) >= MAX_PROMOZIONI:
            print(f"  ..  {_maschera(isin):14} oltre il tetto di {MAX_PROMOZIONI}, alla prossima corsa")
            continue

        try:
            # Francoforte per prima quando la chiave e' un ISIN: prende l'ISIN diretto,
            # quindi niente ricerca del simbolo da indovinare, e conosce i bond.
            tipo = None
            p = ccy = sim = None
            if ISIN.match(isin):
                p, ccy, tipo = da_francoforte(isin)
                time.sleep(0.4)
            if not p:
                sim = cerca_simbolo(isin)
                time.sleep(0.7)                      # non martellare la fonte
                if not sim:
                    raise RuntimeError("nessun simbolo")
                p, ccy = prezzo_yahoo(sim)
                time.sleep(0.7)
            if not p or p <= 0:
                raise RuntimeError("nessun prezzo")
            # Il simbolo serve ad aggiorna.py per le corse successive, ma solo per cio'
            # che passa da Yahoo: i bond li prende dall'export e le loro voci in elenco
            # hanno infatti ticker vuoto. Quindi non si cerca per i bond, e quando la
            # ricerca non trova nulla si scrive "" invece di lasciare None.
            if not sim and tipo != "bond":
                sim = cerca_simbolo(isin)
                time.sleep(0.7)
            sim = sim or ""
            # Stesso strumento gia' coperto sotto un'altra chiave: e' il caso di
            # "ENEL.MI" contro "BIT:ENEL". Si segna come trattato ‚Äî la richiesta ha
            # avuto la sua risposta ‚Äî ma non si aggiunge una seconda voce.
            if sim and sim.upper() in ticker_noti:
                trattati.append(isin)
                print(f"  --  {isin:14} gia' coperto da {sim}, non duplico")
                continue
        except Exception as e:                       # noqa: BLE001
            # Un titolo che non quota oggi pu√≤ quotare domani: non si scarta subito.
            # Solo dopo SCADENZA_GIORNI senza mai un prezzo valido si arrende.
            if _eta_giorni(v, oggi_data) >= SCADENZA_GIORNI:
                trattati.append(isin)
                print(f"  üóë  {_maschera(isin):14} mai un prezzo in {SCADENZA_GIORNI}g, scartato")
            else:
                print(f"  ERR {_maschera(isin):14} {e}")
            continue

        # Il tipo non si inventa piu': "bond" quando Francoforte dice che quota in
        # percentuale del nominale, altrimenti "etf" come prima. Sbagliarlo non e'
        # estetico: aggiorna.py salta Yahoo sui bond, quindi un bond marchiato "etf"
        # verrebbe interrogato a una fonte che non lo conosce, fallirebbe a ogni corsa
        # e dopo dieci finirebbe fra i dismessi.
        strumenti.append({
            "isin": isin, "ticker": sim, "tipo": tipo or "etf",
            "nota": "aggiunto da richiesta utente",
            "aggiunto": "auto", "dal": oggi, "errori_consecutivi": 0,
        })
        if sim:
            ticker_noti.add(sim.upper())
        noti.add(isin)
        promossi.append(isin)
        trattati.append(isin)
        print(f"  OK  {isin:14} {(sim or '‚Äî'):14} {p} {ccy}  ‚Üí promosso come {tipo or 'etf'}")

    if promossi:
        salva(CONFIG, strumenti)
        print(f"\n{len(promossi)} strumenti promossi: {', '.join(promossi)}")
    else:
        print("\nNessuna promozione in questa corsa.")

    # Si pulisce SOLO ci√≤ che √® stato davvero trattato: quello che aspetta ancora
    # deve restare, altrimenti il conteggio dei giorni distinti riparte da zero.
    if trattati:
        try:
            http(f"{WORKER}/pending/pulisci?token={urllib.parse.quote(TOKEN)}",
                 dati={"isin": trattati})
            print(f"Registro ripulito di {len(trattati)} voci.")
        except Exception as e:                       # noqa: BLE001
            # Non √® grave: alla prossima corsa si ritrovano e vengono saltati come
            # "gi√† noti". Meglio una voce di troppo che una richiesta persa.
            print(f"Pulizia del registro fallita ({_senza_token(e)}): si riprover√†.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
