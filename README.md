# listino-prezzi

Listino pubblico di prezzi di chiusura, aggiornato da solo due volte al giorno nei
giorni feriali. Serve a una dashboard di portafoglio che gira come file locale nel
browser: siccome un browser non può leggere direttamente i siti di quotazioni, qui
i prezzi vengono raccolti a monte e pubblicati in un file che invece può leggere.

**Indirizzo del listino:**

```
https://raw.githubusercontent.com/git-alez/listino-prezzi/main/prezzi.json
```

## Cosa c'è dentro

```json
{
  "aggiornato_al": "2026-11-17T16:30:00+00:00",
  "fonti": { "stfi": { "stato": "ok", "ok": 1060 }, "francoforte": { "stato": "ok", "ok": 9 } },
  "prezzi": {
    "IT0005534141": { "p": 98.64, "d": "2026-11-17", "src": "stfi", "ccy": "EUR" }
  }
}
```

`p` è il prezzo, `d` la data a cui si riferisce (una chiusura, non l'istante in cui
è stato scaricato), `ccy` la valuta, `src` la fonte. Le obbligazioni sono espresse in
percentuale del nominale.

## Da dove arrivano i prezzi

L'unica chiave è l'ISIN, e ogni fonte lo accetta direttamente: nessun simbolo da
indovinare.

- **Obbligazioni quotate a Milano**: l'export di fine giornata di
  simpletoolsforinvestors.eu. Entrano tutte, non solo quelle in elenco.
- **Tutto il resto** (azioni, ETF, ETC, obbligazioni estere): la Borsa di Francoforte,
  e dove non risponde justETF. Entrambe in euro.

Quando due fonti quotano lo stesso titolo con più del 3% di differenza, il rapporto
della corsa lo segnala.

## Aggiungere uno strumento

Apri `config/strumenti.json`, aggiungi una riga con il suo ISIN e salva.

```json
{"isin": "IE00B5BMR087", "tipo": "etf", "nota": "iShares Core S&P 500"}
```

Per vedere subito il risultato senza aspettare l'orario: scheda **Actions** →
*Aggiorna listino prezzi* → **Run workflow**.

Gli strumenti che il listino non contiene la dashboard li chiede al worker, che
risponde per qualunque ISIN: aggiungerli qui serve solo ad averli anche quando il
worker non c'è.

## Se un prezzo non arriva

Ogni corsa stampa un rapporto leggibile nella scheda Actions, riga per riga. Uno
strumento che oggi non ha prezzo tiene quello dell'ultima volta, con la sua data: la
dashboard lo mostra come vecchio, invece di farlo sparire.

Se una fonte non risponde, il listino non viene svuotato: un file vecchio è meglio
di un file vuoto.

## Note

I dati provengono da fonti pubbliche non ufficiali e possono cambiare o sparire
senza preavviso. Il listino è offerto senza garanzie e non costituisce consulenza
finanziaria.
