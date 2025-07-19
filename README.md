# Procesador de Documentos - Cloud Function

Un microservicio inteligente implementado como **Google Cloud Function** para la extracción avanzada de entidades de documentos de identificación colombianos utilizando análisis espacial, OCR y almacenamiento en la nube.

## 🎯 ¿Qué hace este microservicio?

Este microservicio procesa documentos de identificación colombianos (imágenes y PDFs) y extrae automáticamente información estructurada como nombres, números de documento, fechas, etc. Utiliza tecnologías avanzadas de OCR y análisis espacial para identificar el tipo de documento y extraer datos con alta precisión.

### Funcionalidades principales:
- **Análisis espacial inteligente**: Detecta la posición exacta del texto en el documento
- **Identificación automática**: Reconoce el tipo de documento y lado (para cédulas amarillas)
- **Extracción de entidades**: Obtiene información estructurada de cada campo
- **Detección de documentos incompletos**: Identifica si faltan lados en cédulas amarillas
- **Procesamiento dual**: Maneja tanto imágenes como PDFs
- **Almacenamiento automático**: Guarda archivos y datos en la nube

## 🛠️ Servicios de Google Cloud Utilizados

### Servicios Principales:
- **Google Cloud Functions Gen2**: Ejecución serverless del microservicio
- **Google Cloud Vision API**: OCR avanzado con información espacial
- **Google Cloud Storage**: Almacenamiento de archivos procesados
- **Google Cloud SQL (MySQL)**: Base de datos para entidades extraídas

### Servicios de Respaldo:
- **Tesseract OCR**: OCR alternativo cuando Vision API no está disponible
- **PyMuPDF**: Conversión de PDF a imagen sin dependencias del sistema

## 📄 Tipos de Documentos Soportados

### ✅ Cédula de Ciudadanía Amarilla
- **Lado 1 (Foto)**: Contiene datos básicos y fotografía
- **Lado 2 (Huella)**: Contiene datos complementarios y huella dactilar
- **Detección inteligente**: Identifica documentos incompletos

### ✅ Cédula de Ciudadanía Digital
- **Documento único**: Toda la información en un solo lado
- **Formato moderno**: Cédula plástica con chip

### ✅ Cédula de Extranjería
- **Para extranjeros**: Documento de identificación para no colombianos

### ✅ Pasaporte Colombiano
- **Documento internacional**: Pasaporte emitido por Colombia

## 📊 Entidades Extraídas

| Campo | Descripción | Disponible en |
|-------|-------------|---------------|
| `numero_documento` | Número de identificación | Todos los documentos |
| `apellidos` | Apellidos completos | Todos los documentos |
| `nombres` | Nombres completos | Todos los documentos |
| `fecha_nacimiento` | Fecha de nacimiento | Todos los documentos |
| `lugar_nacimiento` | Lugar de nacimiento | Cédulas y pasaportes |
| `sexo` | Género (M/F) | Todos los documentos |
| `fecha_expedicion` | Fecha de expedición | Todos los documentos |
| `lugar_expedicion` | Lugar de expedición | Todos los documentos |
| `estatura` | Estatura en metros | Cédulas (cuando aplica) |
| `grupo_sanguineo` | Tipo de sangre | Cédulas (cuando aplica) |

## 🚀 Uso del Microservicio

### Ejemplo con Python

```python
import requests
import base64

# Leer archivo y convertir a base64
with open('documento.jpg', 'rb') as f:  # También funciona con .pdf
    file_content = base64.b64encode(f.read()).decode('utf-8')

# URL de tu Cloud Function
url = ""

# Enviar solicitud
response = requests.post(url, json={
    'file': file_content,
    'file_type': 'image',  # 'image' o 'pdf'
    'store_in_gcs': True   # Opcional: almacenar en Google Cloud Storage
})

# Procesar respuesta
result = response.json()

# Verificar si el procesamiento fue exitoso
if result['success']:
    print(f"✅ Tipo de documento: {result['tipo_documento']}")
    print(f"📄 Documento completo: {result['documento_completo']}")
    
    # Mostrar mensaje si hay información importante
    if 'mensaje' in result:
        print(f"ℹ️  {result['mensaje']}")
    
    # Mostrar entidades extraídas
    print("\n📋 Entidades extraídas:")
    for campo, valor in result['entidades_extraidas'].items():
        if valor:
            print(f"  {campo}: {valor}")
    
    # Información adicional
    print(f"\n💾 Guardado en BD: {result['guardado_en_bd']}")
    print(f"☁️  Archivo almacenado: {result['archivo_almacenado']}")
    
else:
    print(f"❌ Error: {result.get('error', 'Error desconocido')}")
```

## 📤 Respuestas de la API

### Documento Completo (Cédula Digital o Pasaporte)

```json
{
  "success": true,
  "tipo_documento": "cedula_digital",
  "documento_completo": true,
  "texto_legible": "REPÚBLICA DE COLOMBIA\nCÉDULA DE CIUDADANÍA...",
  "entidades_extraidas": {
    "numero_documento": "12345678",
    "nombres": "Juan Carlos",
    "apellidos": "Pérez González",
    "fecha_nacimiento": "15/03/1990",
    "lugar_nacimiento": "Bogotá",
    "sexo": "M",
    "estatura": "1.75",
    "grupo_sanguineo": "O+",
    "fecha_expedicion": "20/01/2020",
    "lugar_expedicion": "Bogotá"
  },
  "archivo_almacenado": true,
  "guardado_en_bd": true,
  "archivo_url": "gs://bucket/documents/file.png",
  "timestamp": "2024-01-15T10:30:00"
}
```

### Cédula Amarilla Completa (Ambos Lados)

```json
{
  "success": true,
  "tipo_documento": "cedula_amarilla",
  "documento_completo": true,
  "mensaje": "Documento completo. Se procesaron ambos lados de la cédula amarilla.",
  "entidades_extraidas": {
    "numero_documento": "12345678",
    "nombres": "Juan Carlos",
    "apellidos": "Pérez González",
    "fecha_nacimiento": "15/03/1990",
    "lugar_nacimiento": "Bogotá",
    "sexo": "M",
    "estatura": "1.75",
    "grupo_sanguineo": "O+",
    "fecha_expedicion": "20/01/2020",
    "lugar_expedicion": "Bogotá"
  },
  "archivo_almacenado": true,
  "guardado_en_bd": true,
  "timestamp": "2024-01-15T10:30:00"
}
```

### Cédula Amarilla Incompleta

```json
{
  "success": true,
  "tipo_documento": "cedula_amarilla",
  "documento_completo": false,
  "lado_faltante": "lado de la huella (lado 2)",
  "mensaje": "Documento incompleto. Falta el lado de la huella (lado 2).",
  "entidades_extraidas": {
    "numero_documento": "12345678",
    "nombres": "Juan Carlos",
    "apellidos": "Pérez González"
    // Solo datos del lado presente
  },
  "archivo_almacenado": true,
  "guardado_en_bd": false,
  "timestamp": "2024-01-15T10:30:00"
}
```

### Error en el Procesamiento

```json
{
  "success": false,
  "error": "Invalid file_type. Must be 'image' or 'pdf'",
  "message": "Detalles adicionales del error"
}
```

## 🔧 Parámetros de Entrada

| Parámetro | Tipo | Requerido | Descripción |
|-----------|------|-----------|-------------|
| `file` | string | ✅ | Archivo codificado en base64 |
| `file_type` | string | ✅ | Tipo de archivo: `"image"` o `"pdf"` |
| `store_in_gcs` | boolean | ❌ | Almacenar en Google Cloud Storage (default: `true`) |

## 🔍 Campos de Respuesta

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `success` | boolean | Indica si el procesamiento fue exitoso |
| `tipo_documento` | string | Tipo identificado: `cedula_amarilla`, `cedula_digital`, `cedula_extranjeria`, `pasaporte`, `unknown` |
| `documento_completo` | boolean | Si el documento está completo (importante para cédulas amarillas) |
| `lado_faltante` | string | Qué lado falta (solo para cédulas amarillas incompletas) |
| `mensaje` | string | Mensaje informativo sobre el estado del documento |
| `texto_legible` | string | Texto completo extraído del documento |
| `entidades_extraidas` | object | Información estructurada extraída |
| `archivo_almacenado` | boolean | Si el archivo se guardó en Google Cloud Storage |
| `guardado_en_bd` | boolean | Si los datos se guardaron en la base de datos |
| `archivo_url` | string | URL del archivo en Google Cloud Storage |
| `timestamp` | string | Fecha y hora del procesamiento |

## 🏗️ Arquitectura Técnica

### Tecnologías Utilizadas:
- **Python 3.9**: Lenguaje de programación
- **Google Cloud Vision API**: OCR principal con análisis espacial
- **PyMuPDF**: Conversión de PDF a imagen
- **Tesseract OCR**: OCR de respaldo
- **OpenCV**: Preprocesamiento de imágenes
- **MySQL**: Base de datos relacional
- **Flask**: Framework web para Cloud Functions

### Flujo de Procesamiento:
1. **Recepción**: Archivo base64 → Decodificación
2. **Conversión**: PDF → Imagen (si aplica)
3. **OCR**: Extracción de texto con coordenadas espaciales
4. **Análisis**: Identificación de tipo de documento y regiones
5. **Extracción**: Obtención de entidades usando análisis espacial
6. **Almacenamiento**: Archivo en GCS y datos en MySQL
7. **Respuesta**: JSON estructurado con resultados

## 📋 Archivos del Proyecto

### Archivos Principales:
- ✅ [`main.py`](main.py:1) - Función principal con lógica de procesamiento
- ✅ [`requirements.txt`](requirements.txt:1) - Dependencias Python


## 💡 Casos de Uso

- **Digitalización de documentos**: Convertir documentos físicos a datos estructurados
- **Verificación de identidad**: Validar información de documentos de identificación
- **Automatización de procesos**: Integrar con sistemas de gestión documental
- **Análisis de documentos**: Procesar grandes volúmenes de documentos automáticamente

---

**Nota**: Este microservicio está optimizado para documentos de identificación colombianos y utiliza análisis espacial avanzado para máxima precisión en la extracción de datos.
