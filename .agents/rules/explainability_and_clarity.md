# Rule: Human-First Architecture & Explainability

## 1. Intuisi & Analogi Sebelum Metrik Teknis
- Setiap kali menjelaskan perubahan arsitektur, algoritma, atau metrik kuantitatif (seperti EV, Walk-Forward CV, Gate Recall, Delayed Queue), agen WAJIB mengawali dengan *alasan bisnis/trading riil* dan analogi sederhana di dunia nyata sebelum menyajikan tabel angka atau diff kode.

## 2. Peta Mental yang Selalu Terjaga
- Setiap fase besar wajib dipetakan kembali ke diagram alir sederhana:
  `Raw Token Ingestion -> Safety Filter -> Scoring Engine -> Delayed Queue -> Alert / Paper Trade`.
- Jangan pernah memperlakukan sistem seperti *black-box*. Pengguna harus selalu memahami *mengapa* suatu token lolos atau ditolak.

## 3. Checkpoint Edukatif & Refleksi
- Sebelum melompat ke fase eksekusi berikutnya, agen harus memastikan pengguna memahami konsekuensi praktis dari perubahan tersebut pada bot saat berjalan di pasar nyata.
- Berikan ruang untuk tanya jawab dan verifikasi kenyamanan pengguna sebelum melanjutkan eksekusi teknis berat.
