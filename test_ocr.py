from ocr_engine import smart_ocr

text = smart_ocr("test.jpg")

print("\n===== OCR OUTPUT =====\n")
print(text)