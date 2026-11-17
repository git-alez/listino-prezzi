#!/usr/bin/env python3
"""
Costruisce prezzi.json: il listino pubblico letto dalla dashboard.

Una chiave sola, l'ISIN, e fonti che lo accettano direttamente:
- obbligazioni: l'export CSV di fine giornata di simpletoolsforinvestors.eu, che
  contiene i titoli quotati su Borsa Italiana con prezzo, rendimento e duration;
- tutto il resto (azioni, ETF, ETC, e i bond che l'export non ha): la Borsa di
  Francoforte, e dove tace justETF. Entrambe rispondono per ISIN e in euro.

Cosa NON c'e' piu', e perche':
- la ricerca del simbolo Yahoo. Era l'unico passaggio in cui si tirava a indovinare
  (il primo risultato della ricerca: a volte Londra in dollari, a volte in pence), ed
  era la fonte dei prezzi "plausibili ma di un'altra piazza";
- il ciclo di vita degli strumenti (contatori di errori, dismissioni, promozioni dal
  registro del worker). config/strumenti.json e' un elenco scritto a mano: chi non ha
  prezzo oggi tiene quello di ieri con la sua data, e la dashboard lo segnala.

Principi rimasti:
- nessuna dipendenza esterna (solo libreria standard): niente da installare;
- se una fonte non risponde NON si sovrascrive il buono con il vuoto: il file
  precedente sopravvive sempre, e lo stato lo dice.
"""

import json, os, re, sys, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

QUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(QUI, "config", "strumenti.json")
USCITA = os.path.join(QUI, "prezzi.json")

# Scarto oltre il quale due fonti sullo stesso titolo meritano un'occhiata. Largo di
# proposito: fra due piazze mezzo punto di differenza e' normale (sono due libri
# d'ordini diversi), e un allarme che suona ogni giorno non lo guarda piu' nessuno.
# Serve a scoprire il prezzo sbagliato di brutto, non la differenza di piazza.
SCARTO_MAX = 3.0
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
# Sotto questo numero di obbligazioni l'export non e' credibile e la corsa lo tratta
# come un guasto. Il listino ne pubblica oltre mille da sempre: cento e' un
# pavimento larghissimo, scelto per non scattare mai su una giornata magra vera.
MIN_BOND = 100

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

UA = "Mozilla/5.0 (compatible; listino-prezzi/2.0; +https://github.com/git-alez/listino-prezzi)"


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



# ------------------------------------------- fonte 2: tutto il resto, per ISIN
def oggi_iso():
    return datetime.now(timezone.utc).date().isoformat()


def da_francoforte(isin):
    """Prezzo per ISIN dalla Borsa di Francoforte: {p, d, ccy, src} oppure None.

    Prende l'ISIN diretto, quindi non c'e' nessun simbolo da indovinare, e quota tutto
    in euro, titoli esteri compresi: la valuta e' nota per costruzione.
    Non solleva mai: una fonte che tace non deve fermare le altre.
    """
    try:
        d = http_json("https://api.boerse-frankfurt.de/v1/data/quote_box/single?isin="
                      + urllib.parse.quote(isin), tentativi=2)
    except Exception:                                # noqa: BLE001
        return None
    p = d.get("lastPrice") if isinstance(d, dict) else None
    if not isinstance(p, (int, float)) or p <= 0:
        return None
    # La data e' quella dell'ULTIMO SCAMBIO, non quella della corsa: un titolo poco
    # scambiato puo' avere un prezzo di giorni fa, e timbrarlo con oggi lo farebbe
    # passare per fresco spegnendo l'avviso di prezzo stantio della dashboard.
    return {"p": round(p, 4), "d": (d.get("timestampLastPrice") or "")[:10] or oggi_iso(),
            "ccy": "EUR", "src": "francoforte"}


def da_justetf(isin):
    """Prezzo per ISIN da justETF (ETF, ETC e molte azioni), in euro. None se non lo ha."""
    try:
        d = http_json("https://www.justetf.com/api/etfs/" + urllib.parse.quote(isin)
                      + "/quote?locale=it&currency=EUR&isin=" + urllib.parse.quote(isin),
                      tentativi=2)
    except Exception:                                # noqa: BLE001
        return None
    p = ((d or {}).get("latestQuote") or {}).get("raw") if isinstance(d, dict) else None
    if not isinstance(p, (int, float)) or p <= 0:
        return None
    return {"p": round(p, 4), "d": d.get("latestQuoteDate") or oggi_iso(),
            "ccy": "EUR", "src": "justetf"}


def plausibile(rec, tipo):
    """Scarta solo l'assurdo (uno zero, un milione): il resto lo giudica il secondo parere."""
    if not rec:
        return False
    return 1 <= rec["p"] <= 300 if tipo == "bond" else 0.0001 <= rec["p"] <= 1_000_000


def due_pareri(s):
    """(isin, tipo, francoforte, justetf) per uno strumento in elenco."""
    isin, tipo = s["isin"], s.get("tipo") or "azione"
    f = da_francoforte(isin)
    # justETF non tratta obbligazioni: chiederglielo sarebbe solo una richiesta a vuoto.
    j = da_justetf(isin) if tipo != "bond" else None
    return isin, tipo, f if plausibile(f, tipo) else None, j if plausibile(j, tipo) else None


# ---------------------------------------------------------------------- corsa
def main():
    strumenti = [s for s in carica(CONFIG, []) if s.get("isin")]
    vecchio = carica(USCITA, {"prezzi": {}})
    prezzi = dict(vecchio.get("prezzi") or {})
    stato = {}

    # --- obbligazioni: una sola richiesta per tutte -------------------------
    print("Obbligazioni (export EOD simpletoolsforinvestors):")
    try:
        bond = bond_da_stfi()
        # Un export vuoto non solleva niente: l'intestazione c'e', le righe no. Senza
        # questo controllo lo stato resterebbe "ok" e la purga in fondo troverebbe
        # "non legittime" tutte le obbligazioni, cancellandole. E non serve un guasto
        # di rete per arrivarci: basta un file troncato, o un cambio di unita' del
        # prezzo che faccia scartare ogni riga. Sollevando qui si ricade sul ramo
        # "errore": niente purga, restano i prezzi della corsa precedente.
        if len(bond) < MIN_BOND:
            raise RuntimeError(
                f"solo {len(bond)} obbligazioni nell'export (soglia {MIN_BOND}): "
                f"lo tratto come guasto della fonte, non come delisting di massa")
        prezzi.update(bond)
        stato["bond"] = {"stato": "ok", "titoli": len(bond)}
        print(f"  {len(bond)} obbligazioni pubblicate")
    except Exception as e:                           # noqa: BLE001
        bond = {}
        stato["bond"] = {"stato": "errore", "msg": str(e)}
        print(f"  ERRORE: {e} — restano i prezzi della corsa precedente")

    # --- il resto: cio' che l'export non ha, per ISIN ------------------------
    # In parallelo: sono richieste indipendenti verso fonti che non frenano (misurate
    # venti richieste a Francoforte in meno di mezzo secondo). Era Yahoo a imporre le
    # pause fra una richiesta e l'altra, e Yahoo non c'e' piu'.
    da_chiedere, saltati = [], []
    for s in strumenti:
        if s["isin"] in bond:
            continue
        (da_chiedere if ISIN_RE.match(s["isin"]) else saltati).append(s)
    for s in saltati:
        print(f"  --  {s['isin']:14} non e' un ISIN: il listino lavora solo per ISIN")

    print(f"\nAzioni, ETF, ETC e bond fuori export ({len(da_chiedere)}), per ISIN:")
    with ThreadPoolExecutor(max_workers=8) as pool:
        esiti = list(pool.map(due_pareri, da_chiedere))

    conta = {"francoforte": 0, "justetf": 0}
    fermi, sospetti = [], []
    for isin, tipo, f, j in esiti:
        scelto = f or j
        if not scelto:
            fermi.append(isin)
            vecchia = (prezzi.get(isin) or {}).get("d", "mai avuto")
            print(f"  --  {isin:14} nessuna fonte risponde (ultimo prezzo: {vecchia})")
            continue
        prezzi[isin] = scelto
        conta[scelto["src"]] += 1
        print(f"  ok  {isin:14} {scelto['src']:12} {scelto['p']:>12.4f} EUR ({scelto['d']})")
        # Secondo parere: un prezzo sbagliato ma verosimile e' il difetto che non si
        # vede. Con due fonti indipendenti nella stessa valuta lo si puo' dire, senza
        # mai bloccare la pubblicazione: un controllo che puo' rifiutare i prezzi
        # diventa lui stesso una causa di guasto.
        if f and j:
            scarto = abs(f["p"] - j["p"]) / j["p"] * 100
            if scarto > SCARTO_MAX:
                sospetti.append((isin, f["p"], j["p"], scarto))

    stato["francoforte"] = {"stato": "ok" if conta["francoforte"] else "errore", "ok": conta["francoforte"]}
    stato["justetf"] = {"stato": "ok" if any(j for *_, j in esiti) else "errore", "ok": conta["justetf"]}
    # Chi legge il listino vuole sapere quali prezzi NON sono stati rinfrescati oggi:
    # il file riporta avanti il valore vecchio, e un prezzo vecchio che sembra buono
    # e' proprio il difetto da far vedere.
    stato["non_aggiornati"] = len(fermi)
    stato["controllo"] = {"sospetti": len(sospetti)}
    if sospetti:
        print(f"\nDa guardare — {len(sospetti)} prezzi che le due fonti raccontano diversi:")
        for isin, p1, p2, sc in sospetti:
            print(f"  ?   {isin:14} Francoforte {p1:>10} / justETF {p2:>10}  ({sc:.1f}%)")

    # Se e' fallito tutto e' piu' probabile un guasto delle fonti che un delisting di
    # massa: non si pubblica e il file buono resta dov'e'.
    if not bond and not any(conta.values()):
        print("\nNessuna fonte ha risposto: prezzi.json NON viene toccato.")
        return 1

    # Il listino porta avanti le chiavi della corsa precedente: uno strumento tolto da
    # strumenti.json lascerebbe il suo ultimo prezzo congelato per sempre. Si tolgono,
    # ma solo se l'export ha risposto: senza questa condizione una giornata di export
    # irraggiungibile renderebbe "orfane" tutte le obbligazioni in un colpo solo.
    if bond:
        leciti = set(bond) | {s["isin"] for s in strumenti}
        orfani = sorted(k for k in prezzi if k not in leciti)
        for k in orfani:
            del prezzi[k]
        if orfani:
            print(f"\nTolte {len(orfani)} chiavi orfane (non piu' in elenco): "
                  f"{', '.join(orfani[:8])}{' ...' if len(orfani) > 8 else ''}")

    salva(USCITA, {
        "aggiornato_al": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonti": stato,
        "prezzi": prezzi,
    })
    kb = os.path.getsize(USCITA) // 1024
    print(f"\nFatto: {len(prezzi)} strumenti nel listino ({kb} KB): "
          f"{conta['francoforte']} da Francoforte, {conta['justetf']} da justETF, "
          f"{len(fermi)} fermi al prezzo precedente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
