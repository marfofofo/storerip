# StoreRip — Deploy su VPS (Hostinger / Ubuntu 24)

App Flask single-page che scrapa store WooCommerce/Shopify via API pubblica e
restituisce un CSV ready-to-import. Zero database, zero file di log, zero account.

---

## 1. Requisiti

- VPS Linux (Ubuntu 24 testato), accesso `root` o `sudo`
- Python 3.10+ (`python3 --version`)
- `pip3`

---

## 2. Copiare i file sul VPS (SCP)

Dalla tua macchina locale, dentro la cartella che contiene `storerip/`:

```bash
scp -r storerip root@VPS_IP:/root/
```

Oppure file per file:

```bash
scp storerip/app.py storerip/scraper.py storerip/ai_enhance.py \
    storerip/requirements.txt storerip/.env.example storerip/start.sh \
    root@VPS_IP:/root/storerip/
scp -r storerip/static storerip/templates root@VPS_IP:/root/storerip/
```

---

## 3. Configurare le chiavi

```bash
cd /root/storerip
cp .env.example .env
nano .env            # inserisci ANTHROPIC_API_KEY (se vuoi l'AI enhance) e PORT
```

- Se **non** metti `ANTHROPIC_API_KEY`, l'app funziona comunque: il checkbox
  "AI ENHANCE" appare disabilitato con badge `[ NO KEY ]`.

---

## 3b. (Opzionale) Tuning senza toccare il codice — `config.json`

Se metti un file `config.json` nella root del progetto, viene caricato
all'avvio (`python3 app.py`) e fuso sui default. Parti da `config.json.example`:

```bash
cp config.json.example config.json
nano config.json
```

| Chiave                   | Default | Descrizione                                  |
|--------------------------|---------|----------------------------------------------|
| `max_jobs`               | `5`     | Job di scraping concorrenti (oltre → 429)    |
| `job_ttl_minutes`        | `30`    | Minuti prima del cleanup automatico dei job  |
| `port`                   | `5050`  | Porta di ascolto (la env `PORT` ha priorità) |
| `enhance_rate_limit_sec` | `1`     | Secondi minimi tra le richieste a Claude     |

> `config.json` è opzionale: senza file, valgono i default qui sopra.
> La variabile d'ambiente `PORT` (da `.env`) ha sempre la precedenza sulla
> `port` di `config.json`.

---

## 4. Installare le dipendenze

```bash
pip3 install -r requirements.txt --break-system-packages -q
```

> `--break-system-packages` serve su Ubuntu 24 (PEP 668). In alternativa usa un
> virtualenv:
> ```bash
> python3 -m venv venv && source venv/bin/activate
> pip install -r requirements.txt
> ```

---

## 5. Avviare

### Avvio rapido (foreground)

```bash
python3 app.py
# → http://VPS_IP:5050
```

### Avvio in background con nohup

```bash
cd /root/storerip
nohup python3 app.py > nohup.out 2>&1 &
```

### Con lo script

```bash
chmod +x start.sh
./start.sh
```

---

## 6. Vedere i log

Tutto il debug va su **stdout** (nessun file di log applicativo). Con `nohup`:

```bash
tail -f /root/storerip/nohup.out
```

---

## 7. Killare il processo

```bash
# trova il PID
ps aux | grep "python3 app.py" | grep -v grep

# kill mirato
kill <PID>

# oppure tutti
pkill -f "python3 app.py"
```

---

## 8. Aprire la porta (firewall)

Se usi `ufw`:

```bash
ufw allow 5050/tcp
```

---

## 9. Uso

1. Apri `http://VPS_IP:5050`
2. Incolla l'URL dello store
3. (Opzionale) `DETECT PLATFORM` per auto-rilevare WooCommerce/Shopify
4. Scegli **PLATFORM** (sorgente) e **OUTPUT** (formato CSV)
5. (Opzionale) spunta **AI ENHANCE COPY** se hai la chiave Claude
6. `RUN SCRAPE` → attendi la progress bar → `DOWNLOAD CSV`

---

## Note operative

- **Job in RAM**: i risultati vivono in memoria, max 5 job concorrenti, puliti
  dopo 30 minuti. Un riavvio dell'app azzera tutto.
- **Download = cleanup**: dopo il download il job viene rimosso dalla memoria.
- **Produzione**: per un deploy stabile valuta un reverse proxy (nginx) davanti
  e un process manager (`systemd` o `pm2`) al posto di `nohup`.
