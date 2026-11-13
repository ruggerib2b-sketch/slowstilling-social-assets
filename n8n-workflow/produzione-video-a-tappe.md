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

## Aggiunte per avvicinarlo alla "fabbrica di video" (2026-11-13, sera)

- `Controlli copione` (prima delle voci): trasforma gli apostrofi in accenti veri (perche' -> perché); si ferma se una scena con numeri non ha la fonte in "Dato con fonte"; avvisa se il titolo supera 60 caratteri o non e una domanda.
- Avvisi nelle Note del foglio: quota di grafiche proprie fuori da 30-70% (rischio "contenuto riciclato"), video sotto 8 minuti (niente pubblicita a meta).
- Uscita programmata alle 14:00 ora italiana (prima 17:00).
- Due trigger programmati: ogni notte alle 2 (produzione), ogni mattina alle 8 (caricamento dei video approvati). Il workflow e inattivo: partono solo se lo attivi.

### Cosa non e uguale al documento di riferimento
- Voce: ElevenLabs v3 (scelta gia fatta) invece di Edge TTS.
- Copione e controllo fatti con AI, argomento scelto da scaletta/radar: il copione e gia nel foglio; manca una credenziale AI.
- Immagini generate (Flux/Cloudflare), musica Jamendo/Freesound, altri archivi (Pexels, Wikimedia, musei): non collegati; ora solo Unsplash (Pixabay ha la credenziale ma non e nel flusso).
- Corti verticali, copertina, sottotitoli caricati su YouTube, audio a -18 dB, PC di riserva per la quota: non fatti; dipendono dal servizio sul VPS, che non ho potuto ispezionare.
