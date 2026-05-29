FROM jupyter/scipy-notebook:python-3.11

USER root

# Системные зависимости: OCR, работа с PDF, шрифты, Java для tabula, LibreOffice для конвертаций
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-rus \
      tesseract-ocr-eng \
      poppler-utils \
      ghostscript \
      libgl1 \
      fonts-dejavu \
      fonts-liberation \
      default-jre-headless \
      libreoffice \
 && rm -rf /var/lib/apt/lists/*

USER ${NB_UID}

# Python-библиотеки для работы с документами
RUN pip install --no-cache-dir \
      pandas \
      openpyxl \
      xlsxwriter \
      python-docx \
      python-pptx \
      pdfplumber \
      pypdf \
      pdf2image \
      camelot-py[cv] \
      tabula-py \
      pytesseract \
      Pillow \
      matplotlib \
      seaborn \
      requests
