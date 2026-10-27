# -*- coding: utf-8 -*-
# golden set: (prompt, dispatch expected, premium plan expected)
VAKALAR=[
 # --- short + simple (ATLA correct) ---
 ("README'ye kurulum bolumu ekle", False, False),
 ("utils.py'deki yazim hatasini duzelt", False, False),
 ("degisken adini x'ten toplam_fiyat'a cevir", False, False),
 ("tests klasorune bos __init__.py ekle", False, False),
 ("versiyonu 1.2.3 yap", False, False),
 ("log satirindaki tarih formatini ISO yap", False, False),
 # --- long/multi + medium (GECER correct, not premium) ---
 ("once config.py'ye retry ayari ekle sonra client.py'de kullan ve testleri guncelle", True, False),
 ("api.py ve models.py dosyalarinda User alanlarini yeni semaya gore guncelle, migration yaz", True, False),
 ("1) parser'i yeni formata uyarla 2) eski format icin fallback birak 3) iki ornekle test et", True, False),
 ("uygulamadaki tum print cagrilarini logging'e cevir, seviyeleri dogru sec, cikti formatini koru ve smoke test kos", True, False),
 ("CSV disa aktarma ozelligi ekle: endpoint, servis katmani, testler ve dokumantasyon", True, False),
 ("cache katmanina TTL destegi ekle ve mevcut cagrilari guncelle, testleri genislet", True, False),
 # --- SHORT BUT HARD (leak risk: must be GECER!) ---
 ("auth katmanini yeniden tasarla", True, True),
 ("veritabani semasini normalize et", True, True),
 ("race condition'i bul ve coz", True, True),
 ("bellek sizintisini tespit edip gider", True, True),
 ("modulu asenkron mimariye tasi", True, True),
 ("odeme akisindaki guvenlik acigini kapat", True, True),
 # --- long + hard (GECER + premium) ---
 ("servisi mikroservis mimarisine bol: siparis ve stok ayri servisler olsun, aralarinda kuyruk kullan, geriye uyumlulugu koru", True, True),
 ("mevcut senkron isleme hattini event-driven yapiya refactor et, idempotency garantisi ekle, olceklenebilirligi dusun", True, True),
 ("kimlik dogrulamayi OAuth2'ye tasi: token yenileme, oturum iptali ve eski API anahtarlarinin gecis plani dahil karmasik bir is", True, True),
 # --- long but ACTUALLY simple (false-positive risk) ---
 ("su dosyalarin hepsinde ayni sabiti guncelle: a.py b.py c.py d.py — degeri 5'ten 10'a cek, baska hicbir sey degistirme, test gerekmiyor", True, False),
 ("README ve CHANGELOG ve KURULUM dosyalarina ayni notu ekle: 'v2 icin python 3.12 gerekir' — kopyala yapistir yeterli", True, False),
 ("loglari incele ve bana ozet cikar, kod degisikligi yapma, sadece rapor yaz", False, False),
 # --- B222 (25.09): code names and substrings are not hard words (9 of the 10 PREMIUMs on 23-24.09 came from here) ---
 ("`tests/unit/test_canonical.py`'ye `normalize_timestamp` icin 4 test ekle ve mevcut testleri yesil tut", True, False),
 ("hata ciktisinda traceback yerine anlasilir mesaj goster ve cli testlerini guncelle", True, False),
 ("`cache.py` icindeki race condition'i bul ve coz, `refresh_token` akisini koru", True, True),
]
