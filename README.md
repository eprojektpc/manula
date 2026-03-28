# manual/

Gotowy panel manualny do wrzucenia **do root repo** jako folder `manual/`.

## Co robi
- panel WWW Flask
- wykres świecowy PRO (candles + EMA 9/21/50)
- RSI pod wykresem
- marker wejścia na wykresie
- manualne BUY / SELL po slotach
- licznik zysku/straty live dla otwartych pozycji
- auto TP / SL
- tryb auto bez auto-zakupów (bot zamyka tylko pozycje na TP/SL)
- cykliczny screener działający w tle
- edycja ustawień skanera bezpośrednio w panelu
- historia trade + historia skanera w SQLite

## Jak uruchomić
W repo root:

```bash
cd manual
pip install -r requirements.txt
python3 app.py
```

Panel domyślnie działa na:

```bash
http://IP_SERWERA:5099
```

## Ważne
Ten moduł ma własny mostek do Binance REST, ale mechanika BUY/SELL jest zrobiona pod ten sam model co bot:
- BUY po `quoteOrderQty`
- SELL po ilości base asset z cięciem do `stepSize`
- sprawdzanie `minNotional`, `minQty` i salda

Czyli wrzucasz folder `manual/` do repo i odpalasz niezależnie od starego panelu.

## Uwagi produkcyjne
Najlepiej uruchomić jako **jeden proces** pod `systemd`, żeby background scan i monitor pozycji nie dublowały się między workerami.

Przykład service:

```ini
[Unit]
Description=Manual Scalp Panel
After=network.target

[Service]
WorkingDirectory=/root/twoje-repo/manual
ExecStart=/usr/bin/python3 /root/twoje-repo/manual/app.py
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```
