"""Test MRMS download and render."""
import gzip
import logging
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.client import Config as BotoCoreConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# AWS config - anonymous access
BUCKET = "noaa-mrms-pds"
boto_config = BotoCoreConfig(signature_version=UNSIGNED)


def download_latest_mrms(save_dir: Path = None):
    """Download the most recent MRMS composite reflectivity."""
    s3 = boto3.client('s3', config=boto_config)
    
    # List recent files instead of guessing timestamps
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    
    prefix = f"CONUS/MergedReflectivityAtLowestAltitude_00.50/{date_str}/"
    
    try:
        logger.info(f"Listing {prefix}...")
        response = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix, MaxKeys=30)
        
        if 'Contents' not in response or not response['Contents']:
            logger.error("No MRMS data found for today")
            return None, None
        
        # Sort by last modified time (newest first)
        objects = sorted(response['Contents'], key=lambda x: x['LastModified'], reverse=True)
        
        # Download the most recent
        for obj in objects[:3]:  # Try top 3 most recent
            key = obj['Key']
            filename = key.split('/')[-1]
            
            # Parse timestamp from filename
            # MRMS_MergedReflectivityAtLowestAltitude_00.50_20260309-040044.grib2.gz
            try:
                parts = filename.replace('.grib2.gz', '').split('_')
                timestamp = parts[-1]  # 20260309-040044
                dt = datetime.strptime(timestamp, "%Y%m%d-%H%M%S")
                dt = dt.replace(tzinfo=timezone.utc)
            except Exception as e:
                logger.debug(f"Could not parse timestamp: {e}")
                continue
            
            # Download
            if save_dir is None:
                save_dir = Path("data/grib_cache/mrms")
            save_dir.mkdir(parents=True, exist_ok=True)
            
            gz_path = save_dir / filename
            grib_path = save_dir / filename.replace('.gz', '')
            
            if grib_path.exists():
                logger.info(f"Already have {grib_path}")
                return grib_path, dt
            
            try:
                logger.info(f"Downloading {filename}...")
                s3.download_file(BUCKET, key, str(gz_path))
                
                # Decompress
                with gzip.open(gz_path, 'rb') as f_in:
                    with open(grib_path, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                
                gz_path.unlink()  # Remove .gz
                logger.info(f"Saved to {grib_path}")
                return grib_path, dt
                
            except Exception as e:
                logger.warning(f"Failed to download {key}: {e}")
                continue
        
    except Exception as e:
        logger.error(f"Error listing MRMS: {e}")
    
    logger.error("No recent MRMS data found")
    return None, None


def render_mrms(grib_path: Path, output_path: Path):
    """Render MRMS reflectivity to PNG."""
    import sys
    sys.path.insert(0, '/home/main/weather-viewer')
    
    from pipeline.processor import FieldProcessor
    from pipeline.renderer import OverlayRenderer
    
    processor = FieldProcessor()
    renderer = OverlayRenderer()
    
    # Extract reflectivity
    field = processor.load_field(grib_path, 'reflectivity')
    if field is None:
        logger.error("Could not load reflectivity from MRMS")
        return False
    
    logger.info(f"MRMS reflectivity shape: {field.values.shape}")
    logger.info(f"Range: {np.nanmin(field.values):.1f} to {np.nanmax(field.values):.1f} dBZ")
    
    # Render
    data_dict = {
        'data': field.values,
        'lat': field.lat,
        'lon': field.lon,
    }
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = renderer._render_fill('reflectivity', data_dict, output_path)
    
    if result and output_path.exists():
        logger.info(f"Rendered to {output_path}")
        return True
    return False


if __name__ == "__main__":
    logger.info("Testing MRMS download and render...")
    
    # Download latest
    grib_path, dt = download_latest_mrms()
    
    if grib_path and dt:
        # Render
        output_dir = Path("data/rendered/mrms") / dt.strftime("%Y%m%d%H%M")
        output_path = output_dir / "reflectivity.png"
        
        success = render_mrms(grib_path, output_path)
        
        if success:
            logger.info(f"SUCCESS! Check {output_path}")
        else:
            logger.error("Render failed")
    else:
        logger.error("Download failed")
