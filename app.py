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

# ── YENI: n8n Takip Otomasyonu ──
N8N_WEBHOOK_URL = os.environ.get(
    "N8N_WEBHOOK_URL",
    "https://freyayachting.app.n8n.cloud/webhook/whatsapp-incoming"
)
FATURA_NUMARASI = "whatsapp:+900000000000"  # Fatura otomasyonu sadece bu numaradan calisir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app    = Flask(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

log.info("ANTHROPIC_API_KEY: %s", ANTHROPIC_API_KEY[:12] + "..." if ANTHROPIC_API_KEY else "BOS!")
log.info("TWILIO_AUTH_TOKEN: %s", TWILIO_AUTH_TOKEN[:8] + "..." if TWILIO_AUTH_TOKEN else "BOS!")
log.info("GOOGLE_CREDENTIALS: %s", "VAR" if GOOGLE_CREDENTIALS else "BOS!")
log.info("N8N_WEBHOOK_URL: %s", N8N_WEBHOOK_URL)

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
        veri   = rows[1:]
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

# ── YENI: n8n'e Yonlendirme ───────────────────────────────────────────────

def n8n_ye_yonlendir(form_data):
    try:
        payload = {
            "From": form_data.get("From", ""),
            "Body": form_data.get("Body", ""),
            "ProfileName": form_data.get("ProfileName", ""),
            "WaId": form_data.get("WaId", ""),
            "To": form_data.get("To", ""),
            "AccountSid": form_data.get("AccountSid", ""),
            "MessageSid": form_data.get("MessageSid", ""),
            "NumMedia": form_data.get("NumMedia", "0"),
        }
        log.info("n8n'e yonlendiriliyor: %s -> %s", payload["From"], N8N_WEBHOOK_URL)
        resp = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
        log.info("n8n yaniti: HTTP %d", resp.status_code)
        return resp.status_code == 200
    except Exception as e:
        log.error("n8n yonlendirme hatasi: %s", e)
        return False

# ── Webhook ────────────────────────────────────────────────────────────────

FATURA_KOMUTLARI = {"ozet", "rapor", "summary"}

@app.route("/webhook", methods=["POST"])
def webhook():
    gonderen     = request.form.get("From", "Bilinmeyen")
    medya_sayisi = int(request.form.get("NumMedia", 0))
    metin        = request.form.get("Body", "").strip()
    account_sid  = request.form.get("AccountSid", "")

    log.info("Webhook: gonderen=%s medya=%d metin=%s", gonderen, medya_sayisi, metin)
    log.info("FATURA_NUMARASI=%s | Eslesme=%s", FATURA_NUMARASI, gonderen == FATURA_NUMARASI)
    twiml = MessagingResponse()

    # ══════════════════════════════════════════════════════════════
    # ROUTER: Numaraya gore yonlendir
    # 0533 numarasi → Fatura otomasyonu (mevcut)
    # Diger numaralar → n8n takip otomasyonu
    # ══════════════════════════════════════════════════════════════

    if gonderen != FATURA_NUMARASI:
        log.info("Farkli numara algilandi (%s) → n8n'e yonlendiriliyor", gonderen)
        n8n_basarili = n8n_ye_yonlendir(request.form)

        if not n8n_basarili:
            twiml.message("Mesajiniz alindi, en kisa surede donecegiz!")
            return Response(str(twiml), mimetype="application/xml")

        return Response(str(twiml), mimetype="application/xml")

    # ══════════════════════════════════════════════════════════════
    # FATURA OTOMASYONU (0533 numarasi - mevcut sistem aynen devam)
    # ══════════════════════════════════════════════════════════════

    if medya_sayisi == 0:
        if metin.lower() in FATURA_KOMUTLARI:
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
    log.info("Router aktif: 0533→Fatura | Diger→n8n")
    app.run(host="0.0.0.0", port=5000, debug=False)
