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

import json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

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
    """Scrittura atomica: un'interruzione a metà non deve lasciare un file monco."""
    tmp = percorso + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, percorso)



def dal_worker(chiave):
    """Prezzo e natura di uno strumento, chiesti al worker. (p, valuta, tipo, simbolo).

    Il worker sa gia' fare tutto questo: prende l'ISIN diretto su Francoforte, ripiega
    su Yahoo per i ticker, tiene in cache i simboli risolti. Reimplementarlo qui
    significava tenere allineate due copie della stessa euristica in due linguaggi, e
    prima o poi divergono. Nessun legame nuovo: questo script parla gia' col worker
    per leggere il registro.

    registra=0 e' obbligatorio, non un'ottimizzazione: senza, chiedere il prezzo di un
    candidato ne aggiornerebbe la data di ultima richiesta, e il filtro dei due giorni
    distinti passerebbe da solo per chiunque — il sensore misurerebbe se stesso.
    """
    try:
        d = http(f"{WORKER}/prezzo?isin={urllib.parse.quote(chiave)}&registra=0")
    except Exception:                                # noqa: BLE001
        return None, None, None, None
    rec = (d or [None])[0] if isinstance(d, list) else None
    if not rec or not isinstance(rec.get("prezzo"), (int, float)) or rec["prezzo"] <= 0:
        return None, None, None, None
    # "percentuale" significa quotato in percentuale del nominale, cioe' obbligazione.
    return (rec["prezzo"], rec.get("ccy") or "EUR",
            ("bond" if rec.get("percentuale") else None), rec.get("simbolo") or "")


def main():
    if not WORKER or not TOKEN:
        print("WORKER_URL o WORKER_TOKEN non impostati: nessuna autoespansione.")
        return 0

    try:
        pendenti = http(f"{WORKER}/pending?token={urllib.parse.quote(TOKEN)}")
    except Exception as e:                           # noqa: BLE001
        # Il sensore giù non deve fermare i prezzi: si riprova alla corsa dopo.
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
            # Già in lista (o già dismesso): la richiesta ha fatto il suo lavoro.
            trattati.append(isin)
            print(f"  --  {isin:14} già noto")
            continue

        # Due giorni distinti: le date di prima e ultima richiesta devono differire.
        if v.get("primo") == v.get("ultimo") or (v.get("conta") or 0) < GIORNI_MINIMI:
            if _eta_giorni(v, oggi_data) >= SCADENZA_GIORNI:
                trattati.append(isin)
                print(f"  🗑  {_maschera(isin):14} mai un secondo giorno in {SCADENZA_GIORNI}g, scartato")
            else:
                print(f"  ..  {_maschera(isin):14} chiesto una volta sola, aspetta")
            continue

        if len(promossi) >= MAX_PROMOZIONI:
            print(f"  ..  {_maschera(isin):14} oltre il tetto di {MAX_PROMOZIONI}, alla prossima corsa")
            continue

        try:
            # Una domanda sola al worker: sa lui quale fonte interrogare e come
            # risolvere il simbolo, e la sua risposta dice anche se e' un'obbligazione.
            p, ccy, tipo, sim = dal_worker(isin)
            if not p:
                raise RuntimeError("nessun prezzo da nessuna fonte")
            # Il simbolo serve ad aggiorna.py per le corse successive, ma solo per cio'
            # che passa da Yahoo: i bond li prende dall'export e le loro voci in elenco
            # hanno infatti ticker vuoto. Quindi non si cerca per i bond, e quando la
            # ricerca non trova nulla si scrive "" invece di lasciare None.
            # Il simbolo lo dice il worker: e' vuoto quando ha risposto Francoforte,
            # che lavora per ISIN e un ticker non lo usa. Va bene per due motivi
            # diversi: i bond il ticker non devono averlo affatto (aggiorna.py li
            # prende dall'export), e per gli altri lo riempie aggiorna.py stessa alla
            # prima corsa, che e' la sola a sapere quale simbolo Yahoo le serve.
            # Stesso strumento gia' coperto sotto un'altra chiave: e' il caso di
            # "ENEL.MI" contro "BIT:ENEL". Si segna come trattato — la richiesta ha
            # avuto la sua risposta — ma non si aggiunge una seconda voce.
            if sim and sim.upper() in ticker_noti:
                trattati.append(isin)
                print(f"  --  {isin:14} gia' coperto da {sim}, non duplico")
                continue
        except Exception as e:                       # noqa: BLE001
            # Un titolo che non quota oggi può quotare domani: non si scarta subito.
            # Solo dopo SCADENZA_GIORNI senza mai un prezzo valido si arrende.
            if _eta_giorni(v, oggi_data) >= SCADENZA_GIORNI:
                trattati.append(isin)
                print(f"  🗑  {_maschera(isin):14} mai un prezzo in {SCADENZA_GIORNI}g, scartato")
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
        print(f"  OK  {isin:14} {(sim or '—'):14} {p} {ccy}  → promosso come {tipo or 'etf'}")

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
            print(f"Pulizia del registro fallita ({_senza_token(e)}): si riproverà.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
