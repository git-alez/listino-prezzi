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
  "aggiornato_al": "2026-08-11T16:30:00+00:00",
  "fonti": { "yahoo": { "stato": "ok", "ok": 12, "errori": 2 } },
  "prezzi": {
    "IT0005534141": { "p": 98.64, "d": "2026-08-07", "src": "yahoo", "ccy": "EUR" }
  }
}
```

`p` è il prezzo, `d` la data a cui si riferisce (una chiusura, non l'istante in cui
è stato scaricato), `ccy` la valuta. I bond sono espressi in percentuale del nominale.

## Aggiungere uno strumento

Apri `config/strumenti.json`, aggiungi una riga con il suo ISIN e salva. Il campo
`ticker` si può lasciare vuoto: alla prima corsa lo script cerca da solo il simbolo
corrispondente e se lo memorizza.

```json
{"isin": "IE00B5BMR087", "ticker": "", "tipo": "etf", "nota": "iShares Core S&P 500"}
```

Per vedere subito il risultato senza aspettare l'orario: scheda **Actions** →
*Aggiorna listino prezzi* → **Run workflow**.

## Se un prezzo non arriva

Ogni corsa stampa un rapporto leggibile nella scheda Actions, riga per riga. Uno
strumento che non risponde per dieci corse di fila viene spostato in
`config/dismessi.json` e sparisce dal listino: non viene cancellato, così se era un
guasto passeggero basta rimetterlo in `strumenti.json`.

Se in una corsa **nessun** prezzo viene ottenuto, il listino non viene toccato: è
più probabile che sia la fonte a essere giù, e un file vecchio è meglio di un file
vuoto.

## Note

I dati provengono da un endpoint pubblico non ufficiale e possono cambiare o
sparire senza preavviso. Il listino è offerto senza garanzie e non costituisce
consulenza finanziaria.
