import os
import base64
import json
import logging
from datetime import datetime
from flask import Flask, request, Response
import requests
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from pathlib import Path

# Ortam degiskenleri
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
EXCEL_FILE         = os.environ.get("EXCEL_FILE", "masraflar.xlsx")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app    = Flask(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

log.info("ANTHROPIC_API_KEY: %s", ANTHROPIC_API_KEY[:12] + "..." if ANTHROPIC_API_KEY else "BOS!")
log.info("TWILIO_AUTH_TOKEN: %s", TWILIO_AUTH_TOKEN[:8] + "..." if TWILIO_AUTH_TOKEN else "BOS!")

# ── Excel ──────────────────────────────────────────────────────────────────

HEADERS = ["Tarih","Saat","Kategori","Aciklama","Tutar","Para Birimi",
           "Belge Turu","Satici","Vergi No","KDV","Odeme","Gonderen","Notlar"]

RENKLER = {
    "Market/Gida":"C8E6C9","Fatura":"BBDEFB","Ulasim":"FFE0B2",
    "Saglik":"F8BBD9","Egitim":"E1BEE7","Eglence":"FFF9C4",
    "Giyim":"B2EBF2","Teknoloji":"DCEDC8","Restoran/Kafe":"FFCCBC",
    "Banka Islemi":"CFD8DC","Diger":"F5F5F5",
}

def excel_ac():
    p = Path(EXCEL_FILE)
    if p.exists():
        wb = openpyxl.load_workbook(p)
        return wb, wb.active
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Masraflar"
    baslik_yaz(ws)
    wb.save(p)
    return wb, ws

def baslik_yaz(ws):
    for col, h in enumerate(HEADERS, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill = PatternFill("solid", fgColor="1565C0")
        c.font = Font(color="FFFFFF", bold=True)
        c.alignment = Alignment(horizontal="center")
    genislikler = [12,8,16,30,12,12,14,25,14,10,14,18,20]
    for i, g in enumerate(genislikler, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = g
    ws.freeze_panes = "A2"

def excele_ekle(data, gonderen):
    wb, ws = excel_ac()
    kategori  = data.get("kategori", "Diger")
    renk      = RENKLER.get(kategori, "F5F5F5")
    fill      = PatternFill("solid", fgColor=renk)
    now       = datetime.now()
    satirlar  = [
        data.get("tarih") or now.strftime("%Y-%m-%d"),
        data.get("saat")  or now.strftime("%H:%M"),
        kategori,
        data.get("aciklama", ""),
        data.get("tutar"),
        data.get("para_birimi", "TRY"),
        data.get("belge_turu", ""),
        data.get("satici", ""),
        data.get("vergi_no", ""),
        data.get("kdv_tutari"),
        data.get("odeme_yontemi", ""),
        gonderen,
        data.get("notlar", ""),
    ]
    satir_no = ws.max_row + 1
    for col, val in enumerate(satirlar, 1):
        c = ws.cell(row=satir_no, column=col, value=val)
        c.fill = fill
    wb.save(EXCEL_FILE)
    log.info("Excel'e eklendi: satir=%d tutar=%s", satir_no, data.get("tutar"))
    return satir_no

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

    # Once auth olmadan dene, sonra auth ile
    goruntu = None
    for auth in [None, (account_sid, TWILIO_AUTH_TOKEN)]:
        try:
            r = requests.get(media_url, auth=auth, timeout=30)
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
    gonderen    = request.form.get("From", "Bilinmeyen")
    medya_sayisi = int(request.form.get("NumMedia", 0))
    metin       = request.form.get("Body", "").strip()
    account_sid = request.form.get("AccountSid", "")

    log.info("Webhook: gonderen=%s medya=%d metin=%s", gonderen, medya_sayisi, metin)

    twiml = MessagingResponse()

    if medya_sayisi == 0:
        if metin.lower() in ("ozet", "rapor", "summary"):
            twiml.message(ozet_olustur())
        else:
            twiml.message("Merhaba! Fatura, fis veya dekont fotografi gonder, Excel'e kaydedeyim. 'ozet' yazarak masraflarini gorebilirsin.")
        return Response(str(twiml), mimetype="application/xml")

    sonuclar = []
    for i in range(medya_sayisi):
        url  = request.form.get(f"MediaUrl{i}")
        tur  = request.form.get(f"MediaContentType{i}", "")
        log.info("Medya %d: %s (%s)", i, url, tur)

        if not url or not tur.startswith("image/"):
            sonuclar.append("Bu dosya turunu okuyamiyorum, fotograf gonder.")
            continue

        data = goruntu_analiz(url, account_sid)
        if data:
            satir = excele_ekle(data, gonderen)
            sonuclar.append(
                f"Kaydedildi! {data.get('kategori','?')} - {data.get('tutar','?')} {data.get('para_birimi','TRY')}\n"
                f"Satici: {data.get('satici','')}\n"
                f"Tarih: {data.get('tarih','')} | {data.get('belge_turu','')}\n"
                f"Odeme: {data.get('odeme_yontemi','')}"
            )
        else:
            sonuclar.append("Gorsel okunamadi, daha net fotograf gonder.")

    twiml.message("\n\n".join(sonuclar))
    return Response(str(twiml), mimetype="application/xml")

def ozet_olustur():
    try:
        wb, ws = excel_ac()
        satirlar = list(ws.iter_rows(min_row=2, values_only=True))
        if not satirlar:
            return "Henuz kayitli masraf yok."
        son10 = satirlar[-10:]
        satirlar_str = ["Son 10 Masraf:"]
        toplam = 0
        for r in reversed(son10):
            try: toplam += float(r[4] or 0)
            except: pass
            satirlar_str.append(f"- {r[0]} | {r[2]} | {r[4]} TRY")
        satirlar_str.append(f"\nToplam: {toplam:,.2f} TRY")
        return "\n".join(satirlar_str)
    except Exception as e:
        return f"Ozet alinamadi: {e}"

@app.route("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

if __name__ == "__main__":
    excel_ac()
    log.info("Sunucu basliyor - port 5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
