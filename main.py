import os
import base64
import io
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from flask import Flask, request, jsonify
import functions_framework
from PIL import Image
import pytesseract
import cv2
import numpy as np
from PyPDF2 import PdfReader
import fitz  # PyMuPDF
import mysql.connector
from google.cloud import storage
from google.cloud import vision

# Configure logging for Cloud Functions
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DocumentProcessor:
    def __init__(self):
        # Initialize GCP clients
        self.vision_client = vision.ImageAnnotatorClient()
        self.storage_client = storage.Client()
        self.bucket_name = os.getenv('GCP_BUCKET_NAME', 'document-processing-bucket')
        
        # Database configuration for Cloud SQL
        self.db_config = {
            'host': os.getenv('DB_HOST'),
            'user': os.getenv('DB_USER'),
            'password': os.getenv('DB_PASSWORD'),
            'database': os.getenv('DB_NAME', 'document_processing'),
            'port': int(os.getenv('DB_PORT', 3306))
        }
        
        # Add Cloud SQL connection for serverless
        if os.getenv('CLOUD_SQL_CONNECTION_NAME'):
            self.db_config['unix_socket'] = f"/cloudsql/{os.getenv('CLOUD_SQL_CONNECTION_NAME')}"
    
    def decode_base64_file(self, base64_string: str) -> bytes:
        """Decode base64 string to bytes"""
        try:
            # Remove data URL prefix if present
            if ',' in base64_string:
                base64_string = base64_string.split(',')[1]
            return base64.b64decode(base64_string)
        except Exception as e:
            logger.error(f"Error decoding base64: {str(e)}")
            raise ValueError("Invalid base64 encoding")
    
    def upload_to_gcs(self, file_data: bytes, filename: str) -> str:
        """Upload file to Google Cloud Storage"""
        try:
            bucket = self.storage_client.bucket(self.bucket_name)
            blob = bucket.blob(f"documents/{filename}")
            blob.upload_from_string(file_data)
            return f"gs://{self.bucket_name}/documents/{filename}"
        except Exception as e:
            logger.error(f"Error uploading to GCS: {str(e)}")
            return None
    
    def process_image(self, image_data: bytes) -> Tuple[str, Image.Image, List[Dict]]:
        """Process image and extract text using OCR with spatial information"""
        try:
            # Convert bytes to PIL Image
            image = Image.open(io.BytesIO(image_data))
            text_blocks = []
            
            # Try Google Cloud Vision API first (more reliable in Cloud Functions)
            try:
                vision_image = vision.Image(content=image_data)
                response = self.vision_client.text_detection(image=vision_image)
                texts = response.text_annotations
                
                if texts:
                    # First annotation is the full text
                    full_text = texts[0].description
                    
                    # Extract spatial information from individual text annotations
                    for annotation in texts[1:]:  # Skip first one (full text)
                        if annotation.description.strip():
                            vertices = annotation.bounding_poly.vertices
                            if vertices:
                                # Calculate bounding box
                                x_coords = [v.x for v in vertices]
                                y_coords = [v.y for v in vertices]
                                
                                text_blocks.append({
                                    'text': annotation.description,
                                    'x': min(x_coords),
                                    'y': min(y_coords),
                                    'width': max(x_coords) - min(x_coords),
                                    'height': max(y_coords) - min(y_coords),
                                    'center_x': sum(x_coords) / len(x_coords),
                                    'center_y': sum(y_coords) / len(y_coords)
                                })
                    
                    logger.info(f"Used Google Cloud Vision API for OCR - Found {len(text_blocks)} text blocks")
                else:
                    full_text = ""
                    
            except Exception as vision_error:
                logger.warning(f"Vision API failed, trying Tesseract: {vision_error}")
                
                # Fallback to Tesseract if Vision API fails
                try:
                    # Convert to OpenCV format for preprocessing
                    cv_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
                    gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
                    denoised = cv2.fastNlMeansDenoising(gray)
                    thresh = cv2.adaptiveThreshold(denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                cv2.THRESH_BINARY, 11, 2)
                    processed_image = Image.fromarray(thresh)
                    full_text = pytesseract.image_to_string(processed_image, lang='spa+eng')
                    
                    # For Tesseract, we'll use a simpler spatial analysis
                    # This is a fallback - Vision API provides better spatial data
                    lines = full_text.split('\n')
                    for i, line in enumerate(lines):
                        if line.strip():
                            text_blocks.append({
                                'text': line.strip(),
                                'x': 0,  # Approximate
                                'y': i * 20,  # Approximate line height
                                'width': len(line) * 10,  # Approximate
                                'height': 20,  # Approximate
                                'center_x': len(line) * 5,  # Approximate
                                'center_y': i * 20 + 10  # Approximate
                            })
                    
                    logger.info("Used Tesseract for OCR with approximate spatial data")
                    
                except Exception as tesseract_error:
                    logger.error(f"Both Vision API and Tesseract failed: {tesseract_error}")
                    raise ValueError("OCR processing failed with both Vision API and Tesseract")
            
            return full_text, image, text_blocks
            
        except Exception as e:
            logger.error(f"Error processing image: {str(e)}")
            raise ValueError("Error processing image")
    
    def process_pdf(self, pdf_data: bytes) -> List[Tuple[str, Image.Image, List[Dict]]]:
        """Process PDF and extract text from each page with spatial information using PyMuPDF"""
        try:
            # Open PDF with PyMuPDF
            doc = fitz.open(stream=pdf_data, filetype="pdf")
            results = []
            
            logger.info(f"Processing PDF with {len(doc)} pages using PyMuPDF")
            
            for page_num in range(len(doc)):
                try:
                    # Load the page
                    page = doc.load_page(page_num)
                    
                    # Convert page to image with high DPI for better OCR
                    pix = page.get_pixmap(dpi=300)
                    img_data = pix.tobytes("png")
                    
                    # Convert to PIL Image
                    pil_image = Image.open(io.BytesIO(img_data))
                    
                    # Process the image using our existing OCR pipeline
                    text, processed_image, text_blocks = self.process_image(img_data)
                    results.append((text, processed_image, text_blocks))
                    
                    logger.info(f"Successfully processed PDF page {page_num + 1}: {len(text)} characters, {len(text_blocks)} text blocks")
                    
                except Exception as page_error:
                    logger.error(f"Error processing PDF page {page_num + 1}: {page_error}")
                    # Add empty result for this page to maintain consistency
                    dummy_image = Image.new('RGB', (800, 600), 'white')
                    results.append(("", dummy_image, []))
            
            # Close the document
            doc.close()
            
            if not results:
                raise ValueError("No pages could be processed from PDF")
            
            logger.info(f"Successfully processed PDF: {len(results)} pages")
            return results
            
        except Exception as e:
            logger.error(f"Error processing PDF with PyMuPDF: {str(e)}")
            raise ValueError(f"Error processing PDF: {str(e)}")
    
    def analyze_document_layout(self, text_blocks: List[Dict]) -> Dict:
        """Analyze document layout and identify document type and side"""
        # Get image dimensions (approximate from text blocks)
        if not text_blocks:
            return {'type': 'unknown', 'side': None, 'layout': 'unknown', 'complete': False}
        
        max_x = max(block['center_x'] for block in text_blocks)
        max_y = max(block['center_y'] for block in text_blocks)
        
        # Combine all text for keyword analysis
        all_text = ' '.join(block['text'] for block in text_blocks).upper()
        
        logger.info(f"Analyzing layout - Image dimensions: {max_x}x{max_y}")
        logger.info(f"Combined text for analysis: {all_text[:200]}...")
        
        # Check for multiple document regions (for combined images)
        regions = self._detect_document_regions(text_blocks, max_x, max_y)
        logger.info(f"Detected {len(regions)} document regions")
        
        # Analyze each region for cédula amarilla patterns
        lado1_found = False
        lado2_found = False
        document_info = {'type': 'unknown', 'side': None, 'layout': 'unknown', 'complete': False}
        
        # Check for Cédula Amarilla patterns
        lado1_keywords = ['REPUBLICA DE COLOMBIA', 'IDENTIFICACION PERSONAL', 'CEDULA DE CIUDADANIA']
        lado2_keywords = ['FECHA DE NACIMIENTO', 'LUGAR DE NACIMIENTO', 'FECHA Y LUGAR DE EXPEDICION']
        
        # Check if we have lado 1 patterns
        if all(keyword in all_text for keyword in lado1_keywords):
            lado1_found = True
            logger.info("Detected: Cédula Amarilla - Lado 1 (lado de la foto)")
        
        # Check if we have lado 2 patterns (and NOT lado 1 exclusive patterns)
        if (all(keyword in all_text for keyword in lado2_keywords) and
            'IDENTIFICACION PERSONAL' not in all_text):
            lado2_found = True
            logger.info("Detected: Cédula Amarilla - Lado 2 (lado de la huella)")
        
        # Determine document status
        if lado1_found and lado2_found:
            document_info = {
                'type': 'cedula_amarilla',
                'side': 'both',
                'layout': 'combined',
                'complete': True,
                'regions': regions
            }
            logger.info("Identified: Cédula Amarilla - Ambos lados presentes")
        elif lado1_found:
            document_info = {
                'type': 'cedula_amarilla',
                'side': 1,
                'layout': 'text_left',
                'complete': False,
                'missing_side': 'lado de la huella (lado 2)'
            }
            logger.info("Identified: Cédula Amarilla - Solo lado 1 (falta lado de la huella)")
        elif lado2_found:
            document_info = {
                'type': 'cedula_amarilla',
                'side': 2,
                'layout': 'text_center',
                'complete': False,
                'missing_side': 'lado de la foto (lado 1)'
            }
            logger.info("Identified: Cédula Amarilla - Solo lado 2 (falta lado de la foto)")
        
        # Check other document types if not cédula amarilla
        elif ('CEDULA DE CIUDADANIA' in all_text and
              'REPUBLICA DE COLOMBIA' in all_text and
              'IDENTIFICACION PERSONAL' not in all_text and
              'NUIP' in all_text):
            document_info = {
                'type': 'cedula_digital',
                'side': 1,
                'layout': 'text_right',
                'complete': True
            }
            logger.info("Identified: Cédula Digital")
        
        # Pasaporte
        elif ('PASAPORTE' in all_text or 'PASSPORT' in all_text):
            document_info = {
                'type': 'pasaporte',
                'side': 1,
                'layout': 'text_below_labels',
                'complete': True
            }
            logger.info("Identified: Pasaporte")
        
        # Analyze text distribution for layout confirmation
        if document_info['type'] != 'unknown':
            left_text_count = sum(1 for block in text_blocks if block['center_x'] < max_x * 0.5)
            right_text_count = sum(1 for block in text_blocks if block['center_x'] >= max_x * 0.5)
            
            logger.info(f"Text distribution - Left: {left_text_count}, Right: {right_text_count}")
        
        return document_info
    
    def _detect_document_regions(self, text_blocks: List[Dict], max_x: float, max_y: float) -> List[Dict]:
        """Detect separate document regions in the image (for combined documents)"""
        if not text_blocks:
            return []
        
        # Group text blocks by vertical regions (assuming documents are side by side or top/bottom)
        regions = []
        
        # Check for horizontal split (side by side)
        left_blocks = [block for block in text_blocks if block['center_x'] < max_x * 0.5]
        right_blocks = [block for block in text_blocks if block['center_x'] >= max_x * 0.5]
        
        if len(left_blocks) > 5 and len(right_blocks) > 5:  # Significant text on both sides
            regions.append({
                'position': 'left',
                'blocks': left_blocks,
                'text': ' '.join(block['text'] for block in left_blocks).upper()
            })
            regions.append({
                'position': 'right',
                'blocks': right_blocks,
                'text': ' '.join(block['text'] for block in right_blocks).upper()
            })
            logger.info("Detected horizontal split (side by side documents)")
        
        # Check for vertical split (top/bottom)
        elif len(regions) == 0:
            top_blocks = [block for block in text_blocks if block['center_y'] < max_y * 0.5]
            bottom_blocks = [block for block in text_blocks if block['center_y'] >= max_y * 0.5]
            
            if len(top_blocks) > 5 and len(bottom_blocks) > 5:  # Significant text on both parts
                regions.append({
                    'position': 'top',
                    'blocks': top_blocks,
                    'text': ' '.join(block['text'] for block in top_blocks).upper()
                })
                regions.append({
                    'position': 'bottom',
                    'blocks': bottom_blocks,
                    'text': ' '.join(block['text'] for block in bottom_blocks).upper()
                })
                logger.info("Detected vertical split (top/bottom documents)")
        
        # If no clear split, treat as single region
        if len(regions) == 0:
            regions.append({
                'position': 'full',
                'blocks': text_blocks,
                'text': ' '.join(block['text'] for block in text_blocks).upper()
            })
            logger.info("Single document region detected")
        
        return regions
    
    def identify_document_type(self, text: str) -> str:
        """Identify document type based on extracted text"""
        text_lower = text.lower()
        # Remove extra whitespace and normalize
        text_normalized = re.sub(r'\s+', ' ', text_lower.strip())
        
        # Document type patterns - more flexible
        patterns = {
            'cedula_amarilla': [
                r'rep[úu]blica.*colombia',  # More flexible spacing
                r'c[ée]dula.*ciudadan[íi]a',  # Handle accent variations
                r'registradur[íi]a.*nacional',  # More flexible
                r'tarjeta.*identidad',  # Alternative term
                r'documento.*identidad'  # Generic identity document
            ],
            'cedula_digital': [
                r'rep[úu]blica.*colombia',
                r'documento.*nacional.*identidad',
                r'c[ée]dula.*digital',
                r'chip.*identidad'
            ],
            'cedula_extranjeria': [
                r'c[ée]dula.*extranjer[íi]a',
                r'migraci[óo]n.*colombia',
                r'extranjero',
                r'permiso.*permanencia'
            ],
            'pasaporte': [
                r'pasaporte',
                r'passport',
                r'rep[úu]blica.*colombia',
                r'ministerio.*relaciones.*exteriores',
                r'relaciones.*exteriores'
            ]
        }
        
        # Score each document type
        scores = {}
        for doc_type, type_patterns in patterns.items():
            score = 0
            for pattern in type_patterns:
                if re.search(pattern, text_normalized):
                    score += 1
                    logger.info(f"Patrón '{pattern}' coincide para {doc_type}")
            scores[doc_type] = score
            logger.info(f"Puntuación para {doc_type}: {score}")
        
        # Find the best match
        best_match = max(scores.items(), key=lambda x: x[1])
        if best_match[1] >= 1:  # Require at least 1 pattern match (more lenient)
            logger.info(f"Documento identificado como: {best_match[0]} (puntuación: {best_match[1]})")
            return best_match[0]
        
        logger.info("No se pudo identificar el tipo de documento")
        return 'unknown'
    
    def extract_entities(self, text: str, document_type: str) -> Dict:
        """Extract entities based on document type"""
        entities = {
            'numero_documento': None,
            'apellidos': None,
            'nombres': None,
            'fecha_nacimiento': None,
            'lugar_nacimiento': None,
            'estatura': None,
            'grupo_sanguineo': None,
            'sexo': None,
            'fecha_expedicion': None,
            'lugar_expedicion': None
        }
        
        # Common patterns for all document types - more flexible and robust
        patterns = {
            'numero_documento': [
                r'(?:no\.?\s*|n[úu]m\.?\s*|n[úu]mero\s*|cc\s*:?\s*|documento\s*:?\s*)?(\d{8,12})',
                r'c\.?c\.?\s*:?\s*(\d{8,12})',
                r'identificaci[óo]n\s*:?\s*(\d{8,12})',
                r'(\d{8,12})(?=\s|$)',  # Standalone number sequence
            ],
            'nombres': [
                r'nombres?\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{1,50})(?=\s*(?:apellidos?|fecha|lugar|\n|$))',
                r'primer\s+nombre\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{1,30})',
                r'name\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{1,30})',
                # Pattern for names that appear after common document headers
                r'(?:rep[úu]blica.*colombia.*|c[ée]dula.*ciudadan[íi]a.*)\s*([A-ZÁÉÍÓÚÑ]{2,}\s+[A-ZÁÉÍÓÚÑ]{2,})',
                # Pattern for two consecutive capitalized words (likely names)
                r'([A-ZÁÉÍÓÚÑ]{2,}\s+[A-ZÁÉÍÓÚÑ]{2,})(?=\s*[A-ZÁÉÍÓÚÑ]{2,}\s+[A-ZÁÉÍÓÚÑ]{2,}|\s*\d|\s*$)'
            ],
            'apellidos': [
                r'apellidos?\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{1,50})(?=\s*(?:nombres?|fecha|lugar|\n|$))',
                r'surname\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{1,30})',
                # Pattern for surnames that typically come before names
                r'([A-ZÁÉÍÓÚÑ]{2,}\s+[A-ZÁÉÍÓÚÑ]{2,})(?=\s*[A-ZÁÉÍÓÚÑ]{2,}\s+[A-ZÁÉÍÓÚÑ]{2,})'
            ],
            'fecha_nacimiento': [
                r'(?:fecha\s*(?:de\s*)?)?nacimiento\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
                r'born\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
                r'date\s*(?:of\s*)?birth\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
                r'naci[óo]\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
                # Generic date pattern near birth-related words
                r'(?:nac|birth).*?(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})'
            ],
            'lugar_nacimiento': [
                r'lugar\s*(?:de\s*)?nacimiento\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,\.]{1,50})(?=\s*(?:fecha|sexo|estatura|\n|$))',
                r'place\s*(?:of\s*)?birth\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,\.]{1,30})',
                r'naci[óo].*?en\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,\.]{1,30})'
            ],
            'sexo': [
                r'sexo\s*:?\s*([MFmf])',
                r'g[ée]nero\s*:?\s*([MFmf])',
                r'sex\s*:?\s*([MFmf])',
                r'\b([MF])\b(?=\s*(?:\d|[A-Z]|$))'  # Standalone M or F
            ],
            'fecha_expedicion': [
                r'(?:fecha\s*(?:de\s*)?)?expedici[óo]n\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
                r'expedida\s*(?:el\s*)?:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
                r'issued\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
                r'exp\.\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})'
            ],
            'lugar_expedicion': [
                r'lugar\s*(?:de\s*)?expedici[óo]n\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,\.]{1,50})(?=\s*(?:v[áa]lida|fecha|\n|$))',
                r'expedida\s*(?:en\s*)?:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,\.]{1,30})',
                r'lugar\s*:?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s,\.]{1,30})(?=\s*(?:v[áa]lida|$))'
            ],
            'estatura': [
                r'estatura\s*:?\s*(\d{1,2}[.,]\d{1,2})\s*m?',
                r'height\s*:?\s*(\d{1,2}[.,]\d{1,2})\s*m?',
                r'est\.\s*:?\s*(\d{1,2}[.,]\d{1,2})\s*m?'
            ],
            'grupo_sanguineo': [
                r'rh\s*:?\s*([ABO][+-])',
                r'grupo\s*sangu[íi]neo\s*:?\s*([ABO][+-])',
                r'blood\s*type\s*:?\s*([ABO][+-])',
                r'sangre\s*:?\s*([ABO][+-])',
                r'\b([ABO][+-])\b(?=\s|$)'  # Standalone blood type
            ]
        }
        
        # Extract entities using patterns with detailed logging
        for entity, entity_patterns in patterns.items():
            found = False
            for i, pattern in enumerate(entity_patterns):
                match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
                if match:
                    extracted_value = match.group(1).strip()
                    # Clean up extracted value
                    if entity in ['nombres', 'apellidos']:
                        # Remove extra spaces and ensure proper capitalization
                        extracted_value = ' '.join(extracted_value.split())
                        extracted_value = extracted_value.title()
                    elif entity == 'sexo':
                        # Normalize sex to uppercase
                        extracted_value = extracted_value.upper()
                    elif entity in ['fecha_nacimiento', 'fecha_expedicion']:
                        # Normalize date format
                        extracted_value = extracted_value.replace('.', '/').replace('-', '/')
                    
                    entities[entity] = extracted_value
                    logger.info(f"Entidad '{entity}' extraída: '{extracted_value}' usando patrón {i+1}")
                    found = True
                    break
            
            if not found:
                logger.info(f"No se pudo extraer la entidad '{entity}'")
        
        return entities
    
    def extract_entities_spatial(self, text_blocks: List[Dict], document_info: Dict) -> Dict:
        """Extract entities using spatial analysis based on document type and layout"""
        entities = {
            'numero_documento': None,
            'apellidos': None,
            'nombres': None,
            'fecha_nacimiento': None,
            'lugar_nacimiento': None,
            'estatura': None,
            'grupo_sanguineo': None,
            'sexo': None,
            'fecha_expedicion': None,
            'lugar_expedicion': None
        }
        
        doc_type = document_info['type']
        doc_side = document_info['side']
        
        logger.info(f"Extracting entities for {doc_type} - Side {doc_side}")
        
        if doc_type == 'cedula_amarilla' and doc_side == 'both':
            # Extract from both sides of cédula amarilla
            entities = self._extract_cedula_amarilla_combined(text_blocks, document_info)
        elif doc_type == 'cedula_amarilla' and doc_side == 1:
            entities = self._extract_cedula_amarilla_lado1(text_blocks)
        elif doc_type == 'cedula_amarilla' and doc_side == 2:
            entities = self._extract_cedula_amarilla_lado2(text_blocks)
        elif doc_type == 'cedula_digital':
            entities = self._extract_cedula_digital(text_blocks)
        elif doc_type == 'pasaporte':
            entities = self._extract_pasaporte(text_blocks)
        else:
            # Fallback to generic extraction
            logger.info("Using fallback generic extraction")
            entities = self._extract_generic(text_blocks)
        
        return entities
    
    def _extract_cedula_amarilla_combined(self, text_blocks: List[Dict], document_info: Dict) -> Dict:
        """Extract entities from both sides of Cédula Amarilla in a combined image"""
        entities = {}
        regions = document_info.get('regions', [])
        
        logger.info("Extracting from combined cédula amarilla document")
        
        # Find which region contains lado 1 and lado 2
        lado1_region = None
        lado2_region = None
        
        for region in regions:
            region_text = region['text']
            
            # Check for lado 1 patterns
            if ('REPUBLICA DE COLOMBIA' in region_text and
                'IDENTIFICACION PERSONAL' in region_text and
                'CEDULA DE CIUDADANIA' in region_text):
                lado1_region = region
                logger.info(f"Found lado 1 in {region['position']} region")
            
            # Check for lado 2 patterns
            elif ('FECHA DE NACIMIENTO' in region_text and
                  'LUGAR DE NACIMIENTO' in region_text and
                  'FECHA Y LUGAR DE EXPEDICION' in region_text):
                lado2_region = region
                logger.info(f"Found lado 2 in {region['position']} region")
        
        # Extract from lado 1 if found
        if lado1_region:
            lado1_entities = self._extract_cedula_amarilla_lado1(lado1_region['blocks'])
            entities.update(lado1_entities)
            logger.info(f"Extracted from lado 1: {lado1_entities}")
        
        # Extract from lado 2 if found
        if lado2_region:
            lado2_entities = self._extract_cedula_amarilla_lado2(lado2_region['blocks'])
            entities.update(lado2_entities)
            logger.info(f"Extracted from lado 2: {lado2_entities}")
        
        logger.info(f"Combined extraction result: {entities}")
        return entities
    
    def _find_text_near_keyword(self, text_blocks: List[Dict], keyword: str, direction: str = 'below', max_distance: int = 100) -> str:
        """Find text near a keyword in a specific direction"""
        keyword_blocks = [block for block in text_blocks if keyword.upper() in block['text'].upper()]
        
        if not keyword_blocks:
            return None
        
        # Use the first occurrence of the keyword
        keyword_block = keyword_blocks[0]
        candidates = []
        
        for block in text_blocks:
            if block == keyword_block:
                continue
                
            distance_x = abs(block['center_x'] - keyword_block['center_x'])
            distance_y = abs(block['center_y'] - keyword_block['center_y'])
            
            if direction == 'below' and block['center_y'] > keyword_block['center_y'] and distance_x < max_distance:
                candidates.append((block, distance_y))
            elif direction == 'above' and block['center_y'] < keyword_block['center_y'] and distance_x < max_distance:
                candidates.append((block, distance_y))
            elif direction == 'right' and block['center_x'] > keyword_block['center_x'] and distance_y < max_distance:
                candidates.append((block, distance_x))
            elif direction == 'left' and block['center_x'] < keyword_block['center_x'] and distance_y < max_distance:
                candidates.append((block, distance_x))
        
        if candidates:
            # Return the closest text block
            closest_block = min(candidates, key=lambda x: x[1])[0]
            return closest_block['text']
        
        return None
    
    def _extract_cedula_amarilla_lado1(self, text_blocks: List[Dict]) -> Dict:
        """Extract entities from Cédula Amarilla - Lado 1"""
        entities = {}
        
        # Sort blocks by Y position (top to bottom) and filter left side
        max_x = max(block['center_x'] for block in text_blocks) if text_blocks else 0
        left_blocks = [block for block in text_blocks if block['center_x'] < max_x * 0.6]  # Left 60%
        left_blocks.sort(key=lambda x: x['center_y'])
        
        # Find número de documento (first number after keywords, near "NUMERO")
        numero = self._find_text_near_keyword(text_blocks, 'NUMERO', 'right')
        if numero:
            # Extract just the number
            match = re.search(r'(\d{8,12})', numero)
            if match:
                entities['numero_documento'] = match.group(1)
        
        # Find apellidos (above "APELLIDOS")
        apellidos = self._find_text_near_keyword(text_blocks, 'APELLIDOS', 'above')
        if apellidos:
            entities['apellidos'] = apellidos.title()
        
        # Find nombres (above "NOMBRES")
        nombres = self._find_text_near_keyword(text_blocks, 'NOMBRES', 'above')
        if nombres:
            entities['nombres'] = nombres.title()
        
        logger.info(f"Cédula Amarilla Lado 1 - Extracted: {entities}")
        return entities
    
    def _extract_cedula_amarilla_lado2(self, text_blocks: List[Dict]) -> Dict:
        """Extract entities from Cédula Amarilla - Lado 2"""
        entities = {}
        
        # Find fecha de nacimiento (right of "FECHA DE NACIMIENTO")
        fecha_nac = self._find_text_near_keyword(text_blocks, 'FECHA DE NACIMIENTO', 'right')
        if fecha_nac:
            match = re.search(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})', fecha_nac)
            if match:
                entities['fecha_nacimiento'] = match.group(1).replace('.', '/').replace('-', '/')
        
        # Find lugar de nacimiento (above "LUGAR DE NACIMIENTO")
        lugar_nac = self._find_text_near_keyword(text_blocks, 'LUGAR DE NACIMIENTO', 'above')
        if lugar_nac:
            entities['lugar_nacimiento'] = lugar_nac.title()
        
        # Find estatura (above "ESTATURA")
        estatura = self._find_text_near_keyword(text_blocks, 'ESTATURA', 'above')
        if estatura:
            match = re.search(r'(\d{1,2}[.,]\d{1,2})', estatura)
            if match:
                entities['estatura'] = match.group(1)
        
        # Find grupo sanguíneo (above "G.S. RH")
        grupo_sang = self._find_text_near_keyword(text_blocks, 'G.S', 'above')
        if grupo_sang:
            match = re.search(r'([ABO][+-])', grupo_sang)
            if match:
                entities['grupo_sanguineo'] = match.group(1)
        
        # Find sexo (above "SEXO")
        sexo = self._find_text_near_keyword(text_blocks, 'SEXO', 'above')
        if sexo:
            match = re.search(r'([MF])', sexo.upper())
            if match:
                entities['sexo'] = match.group(1)
        
        # Find fecha y lugar de expedición (above "FECHA Y LUGAR DE EXPEDICION")
        exp_data = self._find_text_near_keyword(text_blocks, 'FECHA Y LUGAR DE EXPEDICION', 'above')
        if exp_data:
            # Try to find date first
            date_match = re.search(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})', exp_data)
            if date_match:
                entities['fecha_expedicion'] = date_match.group(1).replace('.', '/').replace('-', '/')
                # The rest should be the place
                remaining = exp_data.replace(date_match.group(1), '').strip()
                if remaining:
                    entities['lugar_expedicion'] = remaining.title()
        
        logger.info(f"Cédula Amarilla Lado 2 - Extracted: {entities}")
        return entities
    
    def _extract_cedula_digital(self, text_blocks: List[Dict]) -> Dict:
        """Extract entities from Cédula Digital"""
        entities = {}
        
        # Find número de documento (right of "NUIP")
        numero = self._find_text_near_keyword(text_blocks, 'NUIP', 'right')
        if numero:
            match = re.search(r'(\d{8,12})', numero)
            if match:
                entities['numero_documento'] = match.group(1)
        
        # Find apellidos (below "APELLIDOS")
        apellidos = self._find_text_near_keyword(text_blocks, 'APELLIDOS', 'below')
        if apellidos:
            entities['apellidos'] = apellidos.title()
        
        # Find nombres (below "NOMBRES")
        nombres = self._find_text_near_keyword(text_blocks, 'NOMBRES', 'below')
        if nombres:
            entities['nombres'] = nombres.title()
        
        # Find estatura (below "ESTATURA")
        estatura = self._find_text_near_keyword(text_blocks, 'ESTATURA', 'below')
        if estatura:
            match = re.search(r'(\d{1,2}[.,]\d{1,2})', estatura)
            if match:
                entities['estatura'] = match.group(1)
        
        # Find sexo (below "SEXO")
        sexo = self._find_text_near_keyword(text_blocks, 'SEXO', 'below')
        if sexo:
            match = re.search(r'([MF])', sexo.upper())
            if match:
                entities['sexo'] = match.group(1)
        
        # Find fecha de nacimiento (below "FECHA DE NACIMIENTO")
        fecha_nac = self._find_text_near_keyword(text_blocks, 'FECHA DE NACIMIENTO', 'below')
        if fecha_nac:
            match = re.search(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})', fecha_nac)
            if match:
                entities['fecha_nacimiento'] = match.group(1).replace('.', '/').replace('-', '/')
        
        # Find grupo sanguíneo (below "G.S")
        grupo_sang = self._find_text_near_keyword(text_blocks, 'G.S', 'below')
        if grupo_sang:
            match = re.search(r'([ABO][+-])', grupo_sang)
            if match:
                entities['grupo_sanguineo'] = match.group(1)
        
        # Find lugar de nacimiento (below "LUGAR DE NACIMIENTO")
        lugar_nac = self._find_text_near_keyword(text_blocks, 'LUGAR DE NACIMIENTO', 'below')
        if lugar_nac:
            entities['lugar_nacimiento'] = lugar_nac.title()
        
        # Find fecha y lugar de expedición (below "FECHA Y LUGAR DE EXPEDICION")
        exp_data = self._find_text_near_keyword(text_blocks, 'FECHA Y LUGAR DE EXPEDICION', 'below')
        if exp_data:
            # Split by comma
            parts = exp_data.split(',')
            if len(parts) >= 2:
                # First part should be date
                date_match = re.search(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})', parts[0])
                if date_match:
                    entities['fecha_expedicion'] = date_match.group(1).replace('.', '/').replace('-', '/')
                # Second part should be place
                entities['lugar_expedicion'] = parts[1].strip().title()
        
        logger.info(f"Cédula Digital - Extracted: {entities}")
        return entities
    
    def _extract_pasaporte(self, text_blocks: List[Dict]) -> Dict:
        """Extract entities from Pasaporte"""
        entities = {}
        
        # For passport, values are below their labels
        # This is a simplified implementation - can be expanded based on specific passport layout
        
        # Find apellidos (below "APELLIDOS" or "SURNAME")
        apellidos = self._find_text_near_keyword(text_blocks, 'APELLIDOS', 'below') or \
                   self._find_text_near_keyword(text_blocks, 'SURNAME', 'below')
        if apellidos:
            entities['apellidos'] = apellidos.title()
        
        # Find nombres (below "NOMBRES" or "GIVEN NAMES")
        nombres = self._find_text_near_keyword(text_blocks, 'NOMBRES', 'below') or \
                 self._find_text_near_keyword(text_blocks, 'GIVEN NAMES', 'below')
        if nombres:
            entities['nombres'] = nombres.title()
        
        logger.info(f"Pasaporte - Extracted: {entities}")
        return entities
    
    def _extract_generic(self, text_blocks: List[Dict]) -> Dict:
        """Generic extraction fallback"""
        entities = {}
        
        # Combine all text for generic pattern matching
        all_text = ' '.join(block['text'] for block in text_blocks)
        
        # Use basic patterns as fallback
        # Number extraction
        number_match = re.search(r'(\d{8,12})', all_text)
        if number_match:
            entities['numero_documento'] = number_match.group(1)
        
        # Date extraction
        date_match = re.search(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})', all_text)
        if date_match:
            entities['fecha_nacimiento'] = date_match.group(1).replace('.', '/').replace('-', '/')
        
        logger.info(f"Generic extraction - Extracted: {entities}")
        return entities
    
    def save_to_database(self, document_data: Dict) -> bool:
        """Save extracted data to MySQL database"""
        try:
            connection = mysql.connector.connect(**self.db_config)
            cursor = connection.cursor()
            
            # Create table if not exists
            create_table_query = """
            CREATE TABLE IF NOT EXISTS documentos (
                id INT AUTO_INCREMENT PRIMARY KEY,
                tipo_documento VARCHAR(50),
                numero_documento VARCHAR(20),
                apellidos VARCHAR(100),
                nombres VARCHAR(100),
                fecha_nacimiento DATE,
                lugar_nacimiento VARCHAR(100),
                estatura VARCHAR(10),
                grupo_sanguineo VARCHAR(5),
                sexo CHAR(1),
                fecha_expedicion DATE,
                lugar_expedicion VARCHAR(100),
                texto_completo TEXT,
                archivo_url VARCHAR(500),
                fecha_procesamiento TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_tipo_documento (tipo_documento),
                INDEX idx_numero_documento (numero_documento)
            )
            """
            cursor.execute(create_table_query)
            
            # Insert document data
            insert_query = """
            INSERT INTO documentos (
                tipo_documento, numero_documento, apellidos, nombres,
                fecha_nacimiento, lugar_nacimiento, estatura, grupo_sanguineo,
                sexo, fecha_expedicion, lugar_expedicion, texto_completo, archivo_url
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            # Convert date strings to proper format
            fecha_nacimiento = self._parse_date(document_data.get('fecha_nacimiento'))
            fecha_expedicion = self._parse_date(document_data.get('fecha_expedicion'))
            
            values = (
                document_data.get('tipo_documento'),
                document_data.get('numero_documento'),
                document_data.get('apellidos'),
                document_data.get('nombres'),
                fecha_nacimiento,
                document_data.get('lugar_nacimiento'),
                document_data.get('estatura'),
                document_data.get('grupo_sanguineo'),
                document_data.get('sexo'),
                fecha_expedicion,
                document_data.get('lugar_expedicion'),
                document_data.get('texto_completo'),
                document_data.get('archivo_url')
            )
            
            cursor.execute(insert_query, values)
            connection.commit()
            
            cursor.close()
            connection.close()
            
            return True
        except Exception as e:
            logger.error(f"Error saving to database: {str(e)}")
            return False
    
    def _parse_date(self, date_string: str) -> Optional[str]:
        """Parse date string to MySQL format"""
        if not date_string:
            return None
        
        try:
            # Try different date formats
            formats = ['%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%Y/%m/%d']
            for fmt in formats:
                try:
                    date_obj = datetime.strptime(date_string, fmt)
                    return date_obj.strftime('%Y-%m-%d')
                except ValueError:
                    continue
            return None
        except Exception:
            return None

# Initialize document processor
processor = DocumentProcessor()

@functions_framework.http
def process_document(request):
    """
    Cloud Function entry point for document processing
    """
    # Set CORS headers for preflight requests
    if request.method == 'OPTIONS':
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '3600'
        }
        return ('', 204, headers)
    
    # Set CORS headers for main request
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Content-Type': 'application/json'
    }
    
    try:
        # Only accept POST requests
        if request.method != 'POST':
            return (
                json.dumps({'error': 'Only POST method allowed'}),
                405,
                headers
            )
        
        # Get request data
        request_json = request.get_json(silent=True)
        
        if not request_json or 'file' not in request_json:
            return (
                json.dumps({'error': 'No file provided in request body'}),
                400,
                headers
            )
        
        file_base64 = request_json['file']
        file_type = request_json.get('file_type', 'image')  # 'image' or 'pdf'
        store_in_gcs = request_json.get('store_in_gcs', True)  # Default to True for cloud
        
        # Validate file type
        if file_type not in ['image', 'pdf']:
            return (
                json.dumps({'error': 'Invalid file_type. Must be "image" or "pdf"'}),
                400,
                headers
            )
        
        # Decode base64 file
        try:
            file_data = processor.decode_base64_file(file_base64)
        except ValueError as e:
            return (
                json.dumps({'error': str(e)}),
                400,
                headers
            )
        
        # Generate unique filename
        file_id = str(uuid.uuid4())
        file_extension = 'pdf' if file_type == 'pdf' else 'png'
        filename = f"{file_id}.{file_extension}"
        
        # Upload to GCS if requested
        gcs_url = None
        if store_in_gcs:
            gcs_url = processor.upload_to_gcs(file_data, filename)
        
        # Process file based on type with spatial information
        all_text = ""
        text_blocks = []
        try:
            if file_type == 'pdf':
                results = processor.process_pdf(file_data)
                all_text = " ".join([text for text, _, _ in results])
                # For PDF, use text blocks from first page (most documents are single page)
                if results:
                    text_blocks = results[0][2]  # Get text blocks from first page
            else:
                text, _, blocks = processor.process_image(file_data)
                all_text = text
                text_blocks = blocks
        except ValueError as e:
            return (
                json.dumps({'error': str(e)}),
                400,
                headers
            )
        
        # Add detailed logging for debugging
        logger.info("=== TEXTO COMPLETO EXTRAÍDO ===")
        logger.info(f"Longitud del texto: {len(all_text)} caracteres")
        logger.info(f"Número de bloques de texto: {len(text_blocks)}")
        logger.info(f"Texto: {repr(all_text)}")
        logger.info("=== FIN TEXTO EXTRAÍDO ===")
        
        # Analyze document layout and identify type using spatial information
        document_info = processor.analyze_document_layout(text_blocks)
        document_type = document_info['type']
        logger.info(f"Información del documento: {document_info}")
        
        # Extract entities using spatial analysis if document type is identified
        if document_type != 'unknown':
            entities = processor.extract_entities_spatial(text_blocks, document_info)
            logger.info(f"Entidades extraídas (espacial): {entities}")
        else:
            # Fallback to legacy extraction
            logger.info("Usando extracción legacy como respaldo")
            entities = processor.extract_entities(all_text, document_type)
            logger.info(f"Entidades extraídas (legacy): {entities}")
        
        # Prepare document data for database
        document_data = {
            'tipo_documento': document_type,
            'texto_completo': all_text,
            'archivo_url': gcs_url,
            **entities
        }
        
        # Save to database
        db_saved = processor.save_to_database(document_data)
        
        # Prepare response
        response_data = {
            'success': True,
            'tipo_documento': document_type,
            'texto_legible': all_text,
            'entidades_extraidas': entities,
            'archivo_almacenado': gcs_url is not None,
            'guardado_en_bd': db_saved,
            'archivo_url': gcs_url,
            'timestamp': datetime.now().isoformat()
        }
        
        # Add document completeness information for cédula amarilla
        if document_type == 'cedula_amarilla':
            is_complete = document_info.get('complete', False)
            response_data['documento_completo'] = is_complete
            
            if not is_complete:
                missing_side = document_info.get('missing_side')
                if missing_side:
                    response_data['lado_faltante'] = missing_side
                    response_data['mensaje'] = f"Documento incompleto. Falta el {missing_side}."
                    logger.info(f"Cédula amarilla incompleta - falta: {missing_side}")
                else:
                    response_data['mensaje'] = "Documento incompleto. Se requieren ambos lados de la cédula amarilla."
            else:
                response_data['mensaje'] = "Documento completo. Se procesaron ambos lados de la cédula amarilla."
                logger.info("Cédula amarilla completa procesada")
        else:
            # For other document types, they are complete by default
            response_data['documento_completo'] = True
        
        return (
            json.dumps(response_data, ensure_ascii=False),
            200,
            headers
        )
        
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return (
            json.dumps({
                'error': 'Internal server error',
                'message': str(e)
            }),
            500,
            headers
        )

# For local testing with Flask
if __name__ == '__main__':
    from flask import Flask
    app = Flask(__name__)
    
    @app.route('/', methods=['POST', 'OPTIONS'])
    def local_process():
        return process_document(request)
    
    @app.route('/health', methods=['GET'])
    def health():
        return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})
    
    app.run(host='0.0.0.0', port=8080, debug=True)