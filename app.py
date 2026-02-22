import os
import base64
import json
import logging
from datetime import datetime
from flask import Flask, request, Response
import requests
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
from pathlib import Path

# Ortam degiskenleri
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "").strip()
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
SHEET_ID           = "1rxH9p1ct7NM90kCmg8CQOieatkqU-3JyZ2uYK3Z87ZE"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app    = Flask(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

log.info("ANTHROPIC_API_KEY: %s", ANTHROPIC_API_KEY[:12] + "..." if ANTHROPIC_API_KEY else "BOS!")
log.info("TWILIO_AUTH_TOKEN: %s", TWILIO_AUTH_TOKEN[:8] + "..." if TWILIO_AUTH_TOKEN else "BOS!")
log.info("GOOGLE_CREDENTIALS: %s", "VAR" if GOOGLE_CREDENTIALS else "BOS!")

# ── Google Sheets ──────────────────────────────────────────────────────────

HEADERS = ["Tarih","Saat","Kategori","Aciklama","Tutar","Para Birimi",
           "Belge Turu","Satici","Vergi No","KDV","Odeme","Gonderen","Notlar"]

def sheets_baglanti():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.sheet1
    # Baslik yoksa ekle
    if ws.row_count == 0 or ws.cell(1, 1).value != "Tarih":
        ws.insert_row(HEADERS, 1)
    return ws

def sheete_ekle(data, gonderen):
    try:
        ws  = sheets_baglanti()
        now = datetime.now()
        satir = [
            data.get("tarih") or now.strftime("%Y-%m-%d"),
            data.get("saat")  or now.strftime("%H:%M"),
            data.get("kategori", "Diger"),
            data.get("aciklama", ""),
            data.get("tutar", ""),
            data.get("para_birimi", "TRY"),
            data.get("belge_turu", ""),
            data.get("satici", ""),
            data.get("vergi_no", "") or "",
            data.get("kdv_tutari", "") or "",
            data.get("odeme_yontemi", ""),
            gonderen,
            data.get("notlar", ""),
        ]
        ws.append_row(satir)
        log.info("Google Sheets'e eklendi!")
        return True
    except Exception as e:
        log.error("Google Sheets hatasi: %s", e)
        return False

def ozet_olustur():
    try:
        ws   = sheets_baglanti()
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return "Henuz kayitli masraf yok."
        veri   = rows[1:]  # baslik satirini atla
        son10  = veri[-10:]
        satirlar = ["Son 10 Masraf:"]
        toplam = 0
        for r in reversed(son10):
            try: toplam += float(str(r[4]).replace(",", ".") or 0)
            except: pass
            satirlar.append(f"- {r[0]} | {r[2]} | {r[4]} TRY")
        satirlar.append(f"\nToplam: {toplam:,.2f} TRY")
        return "\n".join(satirlar)
    except Exception as e:
        return f"Ozet alinamadi: {e}"

# ── Claude ─────────────────────────────────────────────────────────────────

PROMPT = """Finansal belge analiz uzmanisın. Goruntu bir fatura, fis veya dekont.
Icindeki bilgileri cikart ve SADECE asagidaki JSON formatinda dondur, baska hicbir sey yazma.
Sayisal degerlerde nokta kullan (ornek: 1267.42). Bulamazsan null yaz.

{
  "tarih": "YYYY-MM-DD",
  "saat": "HH:MM",
  "kategori": "Market/Gida veya Fatura veya Ulasim veya Saglik veya Egitim veya Eglence veya Giyim veya Teknoloji veya Restoran/Kafe veya Banka Islemi veya Diger",
  "aciklama": "kisa aciklama",
  "tutar": 0.00,
  "para_birimi": "TRY",
  "belge_turu": "fis veya fatura veya dekont veya makbuz veya diger",
  "satici": "satici veya kurum adi",
  "vergi_no": null,
  "kdv_tutari": null,
  "odeme_yontemi": "nakit veya kredi karti veya banka karti veya havale veya EFT veya diger",
  "notlar": ""
}"""

def goruntu_analiz(media_url, account_sid):
    log.info("Goruntu indiriliyor: %s", media_url)
    goruntu = None
    for auth in [(account_sid, TWILIO_AUTH_TOKEN), None]:
        try:
            r = requests.get(media_url, auth=auth, timeout=30, allow_redirects=True)
            log.info("HTTP %d, boyut=%d, auth=%s", r.status_code, len(r.content), "var" if auth else "yok")
            if r.status_code == 200:
                goruntu = r
                break
        except Exception as e:
            log.error("Indirme hatasi: %s", e)

    if not goruntu:
        log.error("Goruntu indirilemedi!")
        return None

    mime = goruntu.headers.get("Content-Type", "image/jpeg").split(";")[0]
    b64  = base64.standard_b64encode(goruntu.content).decode()

    log.info("Claude'a gonderiliyor...")
    try:
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text",  "text": "Bu belgeyi analiz et."}
                ]
            }]
        )
        ham = msg.content[0].text.strip()
        log.info("Claude yaniti: %s", ham[:300])
        if "```" in ham:
            ham = ham.split("```")[1]
            if ham.startswith("json"):
                ham = ham[4:]
        return json.loads(ham.strip())
    except Exception as e:
        log.error("Claude hatasi: %s", e)
        return None

# ── Webhook ────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    gonderen     = request.form.get("From", "Bilinmeyen")
    medya_sayisi = int(request.form.get("NumMedia", 0))
    metin        = request.form.get("Body", "").strip()
    account_sid  = request.form.get("AccountSid", "")

    log.info("Webhook: gonderen=%s medya=%d metin=%s", gonderen, medya_sayisi, metin)

    twiml = MessagingResponse()

    if medya_sayisi == 0:
        if metin.lower() in ("ozet", "rapor", "summary"):
            twiml.message(ozet_olustur())
        else:
            twiml.message("Merhaba! Fatura, fis veya dekont fotografi gonder, Google Sheets'e kaydedeyim. 'ozet' yazarak masraflarini gorebilirsin.")
        return Response(str(twiml), mimetype="application/xml")

    sonuclar = []
    for i in range(medya_sayisi):
        url = request.form.get(f"MediaUrl{i}")
        tur = request.form.get(f"MediaContentType{i}", "")
        log.info("Medya %d: %s (%s)", i, url, tur)

        if not url or not tur.startswith("image/"):
            sonuclar.append("Bu dosya turunu okuyamiyorum, fotograf gonder.")
            continue

        data = goruntu_analiz(url, account_sid)
        if data:
            basari = sheete_ekle(data, gonderen)
            if basari:
                sonuclar.append(
                    f"Kaydedildi! {data.get('kategori','?')} - {data.get('tutar','?')} {data.get('para_birimi','TRY')}\n"
                    f"Satici: {data.get('satici','')}\n"
                    f"Tarih: {data.get('tarih','')} | {data.get('belge_turu','')}\n"
                    f"Odeme: {data.get('odeme_yontemi','')}\n"
                    f"Google Sheets'e kaydedildi!"
                )
            else:
                sonuclar.append("Veri okundu ama Sheets'e kaydedilemedi, loglari kontrol et.")
        else:
            sonuclar.append("Gorsel okunamadi, daha net fotograf gonder.")

    twiml.message("\n\n".join(sonuclar))
    return Response(str(twiml), mimetype="application/xml")

@app.route("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

if __name__ == "__main__":
    log.info("Sunucu basliyor - port 5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
