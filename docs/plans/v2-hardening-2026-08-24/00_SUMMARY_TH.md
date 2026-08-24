# สรุป
สิ่งที่ยังควรอุดหลัง 1.4.1:
1. Classifier ปัจจุบันยังพึ่ง keyword/text length มากไป
2. ต้อง adaptive escalation ระหว่างทำงาน
3. token budget ต้อง dynamic และวัดจากงานจริง
4. ต้องมี app-wide RAM/CPU/resource governor
5. fail-open/circuit breaker ต้องเป็น policy กลาง
6. Context Debug ต้องอธิบายเหตุผลการเลือก/ข้าม source
7. UX ผู้ใช้ทั่วไปต้องเหลือ Fast/Automatic/Deep
8. Managed Local OpenViking ต้องทำให้ไม่ต้อง Docker/terminal
9. ต้อง benchmark v1-like vs Automatic vs Deep จริง
10. retrieval ต้องป้องกัน prompt injection และรักษา provenance
