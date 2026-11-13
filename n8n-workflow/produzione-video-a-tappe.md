# Slow Stilling - Produzione video (a tappe)

Workflow n8n `jmeffXFO5uCOp7AP` (inattivo, creato il 2026-11-13). Sostituisce il vecchio "YouTube: voci, foto e montaggio della puntata" (`zsdPKbXQQaBE0XsJ`, lasciato intatto).

Nessuna chiave in questo file. La chiave del servizio di montaggio e le credenziali stanno solo in n8n.

## Tappa 1 - Produzione (trigger manuale "Avvio produzione")

1. `Puntata approvata` - Google Sheet, YT Video, prima riga con Stato = `copione approvato`
2. `Scene della puntata` - tab YT Scene
3. `Elenco voci` - ElevenLabs, trova Tiziana (Giulia) e Brando (Marco)
4. `Prepara battute` - una riga per scena, parole di ricerca foto, testo da leggere
5. `Ricerche uniche` -> `Cerca foto` (Unsplash, max 40 ricerche) -> `Foto per scena`
6. `Ciclo battute` (batch 1) -> `ElevenLabs legge la battuta` (v3, retry 5x) -> `Converti in WAV` -> torna al ciclo
   - Differenza chiave rispetto al vecchio: una battuta per volta, convertita subito, nessuna richiesta lunga 13 minuti.
7. `Prepara richiesta di montaggio` (ordina per N) -> `Invia al montaggio` -> `Attendi` 45 s -> `Controlla stato` -> `Segnala errore` -> `Montaggio pronto?` (se no, torna ad Attendi)
8. `Link del video` -> `Prepara aggiornamento` -> `Aggiorna riga video`: Stato = `video da approvare`, link video/srt/capitoli nelle Note, crediti foto in Descrizione

## Tappa 2 - Caricamento YouTube (trigger manuale "Avvio caricamento")

Parte solo da una riga con Stato = `video approvato` (lo imposti tu dopo aver guardato il video).

1. `Video approvato` + `Serie e playlist` (YT Serie, per ID playlist)
2. `Prepara caricamento` - legge il link del video dalle Note, la Data uscita (gg/mm/aaaa o aaaa-mm-gg), calcola publishAt alle 17:00 ora italiana; si ferma se la data e passata o la playlist manca
3. `Scarica il video` dal servizio di montaggio
4. `Carica su YouTube (privato)` - privacy private + publishAt, non per bambini, nessuna notifica
5. `Aggiungi alla playlist`
6. `Verifica il video` - rilegge da YouTube; `Esito caricamento` si ferma se il video non risulta privato
7. `Segna caricato nel foglio` - ID video YouTube, Stato = `caricato (privato, programmato)`

## Da verificare prima del primo uso

- Categoria YouTube impostata a 27 (Istruzione): scelta mia, da confermare.
- Se l'app Google e in modalita "test", il token YouTube puo scadere dopo circa 7 giorni (regola generale Google, non verificata sul progetto).
- Grafiche con testi fonte, miniature e sottotitoli: dipendono dal servizio di montaggio sul VPS, non controllati.
- Licenza del jingle e seconda fonte foto (Pixabay): aperti.
- La chiave X-API-Key del montaggio e scritta in chiaro in 3 nodi HTTP: meglio spostarla in una credenziale Header Auth.
- Mai eseguito: nessuna prova reale ne caricamento fatto.
