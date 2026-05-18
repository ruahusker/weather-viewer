"""GOES-R Series satellite data downloader from AWS S3.

Downloads ABI imagery and GLM lightning data from NOAA's open data buckets.
No AWS credentials required - uses anonymous access.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import threading
from typing import Optional

import time

import numpy as np
import s3fs
import xarray as xr
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# AWS S3 bucket names (public, no auth required)
S3_BUCKETS = {
    16: "noaa-goes16",   # On-orbit storage (data stopped April 2025)
    18: "noaa-goes18",   # GOES-West (137.0°W) - operational
    19: "noaa-goes19",   # GOES-East (75.2°W) - operational since April 2025
}

# Product paths in S3 buckets
PRODUCT_PATHS = {
    "ABI-L2-MCMIP": "ABI-L2-MCMIP",      # Multi-channel CMIP (16 bands)
    "ABI-L2-CMIP": "ABI-L2-CMIP",         # Single channel CMIP
    "ABI-L1b-Rad": "ABI-L1b-Rad",         # Raw radiances
    "GLM-L2-LCFA": "GLM-L2-LCFA",         # Lightning
}

# Domain codes
DOMAIN_CODES = {
    "F": "F",    # Full Disk
    "C": "C",    # CONUS (Continental US)
    "M1": "M1",  # Mesoscale 1
    "M2": "M2",  # Mesoscale 2
}


@dataclass
class GOESFile:
    """Represents a GOES data file."""
    satellite: int  # 16, 18, or 19
    product: str
    domain: str
    timestamp: datetime
    filename: str
    s3_path: str
    local_path: Optional[Path] = None
    
    @property
    def is_glm(self) -> bool:
        return "GLM" in self.product
    
    @property
    def is_abi(self) -> bool:
        return "ABI" in self.product


class GOESDownloader:
    """Download GOES-R ABI imagery from AWS S3.
    
    Example:
        downloader = GOESDownloader(satellite=16, domain="C")
        files = downloader.list_available(hours=2)
        downloader.download_files(files[:5], output_dir="./sat_data/")
    """
    
    def __init__(self, satellite: int = 16, domain: str = "C", product: str = "ABI-L2-MCMIP"):
        """Initialize downloader.
        
        Args:
            satellite: 16 (East), 18 (West), or 19 (future East)
            domain: "F" (Full), "C" (CONUS), "M1", "M2" (Mesoscale)
            product: "ABI-L2-MCMIP", "ABI-L2-CMIP", "ABI-L1b-Rad"
        """
        if satellite not in S3_BUCKETS:
            raise ValueError(f"Invalid satellite {satellite}. Use 16, 18, or 19.")
        
        self.satellite = satellite
        self.domain = domain
        self.product = product
        self.bucket = S3_BUCKETS[satellite]

        # Initialize S3 filesystem with timeouts to prevent indefinite hangs
        self.fs = self._make_fs()
        self._fs_created_at = time.time()
        # Recreate the filesystem every 20 minutes to flush stale connections
        self._fs_refresh_interval = 1200

        logger.info(f"GOESDownloader initialized: GOES-{satellite} {product} domain={domain}")

    @staticmethod
    def _make_fs() -> s3fs.S3FileSystem:
        """Create a fresh S3FileSystem with conservative timeouts."""
        return s3fs.S3FileSystem(
            anon=True,
            use_listings_cache=False,
            config_kwargs={
                "connect_timeout": 15,
                "read_timeout": 60,
                "retries": {"max_attempts": 2, "mode": "standard"},
            },
        )

    def _refresh_fs_if_stale(self):
        """Recreate the S3 filesystem if it hasn't been refreshed recently."""
        if time.time() - self._fs_created_at > self._fs_refresh_interval:
            self.fs = self._make_fs()
            self._fs_created_at = time.time()
            logger.debug("S3 filesystem refreshed")

    def _product_prefix(self) -> str:
        """Return the S3 product prefix for this downloader.

        Mesoscale domains M1/M2 share a single S3 directory suffix 'M',
        so both map to e.g. ABI-L2-MCMIPM. Filtering by M1/M2 happens
        at the filename level in _parse_filename.
        """
        if self.product == "GLM-L2-LCFA":
            return self.product
        # M1 and M2 both live under the 'M' suffix in S3
        domain = "M" if self.domain in ("M1", "M2") else self.domain
        return f"{self.product}{domain}"
    
    def _get_latest_available_date(self) -> Optional[datetime]:
        """Find the most recent date with data in the S3 bucket.
        
        This handles cases where system date is ahead of available data.
        """
        try:
            # List years available
            prefix = f"{self.bucket}/{self._product_prefix()}/"
            years = []
            try:
                year_items = self.fs.ls(prefix)
                years = [int(y.split('/')[-1]) for y in year_items if y.split('/')[-1].isdigit()]
                years = sorted(years)
            except Exception as e:
                logger.debug(f"Could not list years: {e}")
                return None
            
            if not years:
                return None
            
            latest_year = years[-1]
            
            # List days in latest year
            year_prefix = f"{prefix}{latest_year}/"
            try:
                day_items = self.fs.ls(year_prefix)
                days = [int(d.split('/')[-1]) for d in day_items if d.split('/')[-1].isdigit()]
                days = sorted(days)
            except:
                return None
            
            if not days:
                return None
            
            latest_day = days[-1]
            
            # List hours in latest day
            day_prefix = f"{year_prefix}{latest_day:03d}/"
            try:
                hour_items = self.fs.ls(day_prefix)
                hours = [int(h.split('/')[-1]) for h in hour_items if h.split('/')[-1].isdigit()]
                hours = sorted(hours)
            except:
                return None
            
            if not hours:
                return None
            
            latest_hour = hours[-1]
            
            # Construct datetime
            date = datetime(latest_year, 1, 1, latest_hour, 0, 0, tzinfo=timezone.utc)
            date = date + timedelta(days=latest_day - 1)
            
            logger.info(f"Latest available data: {date.strftime('%Y-%m-%d %H:%M')} UTC")
            return date
            
        except Exception as e:
            logger.warning(f"Error finding latest date: {e}")
            return None
    
    def _parse_filename(self, filename: str, s3_path: str) -> Optional[GOESFile]:
        """Parse GOES filename to extract metadata.
        
        Filename format:
        OR_ABI-L2-MCMIPC-M6_G16_s20251581250209_e20251581259517_c20251581300031.nc
        """
        if self.product == "GLM-L2-LCFA":
            pattern = (
                r'OR_(?P<product>GLM-L2-LCFA)_G(?P<sat>\d+)_'
                r's(?P<start>\d+)_e\d+_c\d+\.nc'
            )
            match = re.match(pattern, filename)
            default_domain = "F"
        else:
            # Pattern matches both L2 (e.g., OR_ABI-L2-CMIPC-M6_G19_) and
            # L1b (e.g., OR_ABI-L1b-RadC-M6C02_G19_).
            pattern = (
                r'OR_(?P<product>.+?)(?P<domain>F|C|M1|M2)-M\d+(?:C\d+)?_G(?P<sat>\d+)_'
                r's(?P<start>\d+)_e\d+_c\d+\.nc'
            )
            match = re.match(pattern, filename)
            default_domain = self.domain
        if not match:
            return None
        
        groups = match.groupdict()
        start_str = groups['start']
        
        # Parse timestamp from filename (YYYYDDDHHMMSSf)
        try:
            year = int(start_str[0:4])
            day_of_year = int(start_str[4:7])
            hour = int(start_str[7:9])
            minute = int(start_str[9:11])
            second = int(start_str[11:13])
            
            timestamp = datetime(year, 1, 1, hour, minute, second, tzinfo=timezone.utc) + \
                       timedelta(days=day_of_year - 1)
        except (ValueError, IndexError) as e:
            logger.warning(f"Failed to parse timestamp from {filename}: {e}")
            return None
        
        return GOESFile(
            satellite=int(groups['sat']),
            product=groups['product'],
            domain=groups.get('domain', default_domain),
            timestamp=timestamp,
            filename=filename,
            s3_path=s3_path
        )
    
    def list_available(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        hours: Optional[int] = None,
        max_files: int = 100
    ) -> list[GOESFile]:
        """List available GOES files in time range.
        
        Args:
            start_time: Start of time range (UTC)
            end_time: End of time range (UTC)
            hours: Alternative to start_time - go back N hours from now
            max_files: Maximum files to return
        
        Returns:
            List of GOESFile objects, sorted by timestamp (newest first)
        """
        self._refresh_fs_if_stale()

        if end_time is None:
            end_time = datetime.now(timezone.utc)

        if start_time is None:
            if hours:
                start_time = end_time - timedelta(hours=hours)
            else:
                start_time = end_time - timedelta(hours=1)

        # Note: Removed latest_available adjustment as it was causing issues
        # near hour boundaries. The while loop will naturally stop when no files
        # are found in future hours.
        
        files = []
        
        # Build S3 prefix path: bucket/product/year/day/hour/
        current = start_time.replace(minute=0, second=0, microsecond=0)
        
        while current <= end_time:
            year = current.year
            day_of_year = current.timetuple().tm_yday
            hour = current.hour
            
            prefix = f"{self.bucket}/{self._product_prefix()}/{year}/{day_of_year:03d}/{hour:02d}/"
            
            try:
                logger.debug(f"Listing S3 prefix: {prefix}")
                # s3fs caches directory listings by default; force a fresh view
                # so newly-published GOES files are visible without a restart.
                self.fs.invalidate_cache(prefix)
                s3_files = self.fs.ls(prefix, refresh=True)
                
                for s3_path in s3_files:
                    if not s3_path.endswith('.nc'):
                        continue
                    
                    filename = s3_path.split('/')[-1]
                    goes_file = self._parse_filename(filename, s3_path)
                    
                    if goes_file and start_time <= goes_file.timestamp <= end_time:
                        # For mesoscale, filter to the requested M1/M2 domain
                        if self.domain in ("M1", "M2") and goes_file.domain != self.domain:
                            continue
                        files.append(goes_file)
                            
            except FileNotFoundError:
                logger.debug(f"No files found for prefix: {prefix}")
            except Exception as e:
                logger.warning(f"Error listing S3 prefix {prefix}: {e}")
                
            current += timedelta(hours=1)
        
        # Sort by timestamp, newest first, then limit to max_files
        files.sort(key=lambda x: x.timestamp, reverse=True)
        if len(files) > max_files:
            files = files[:max_files]
        
        logger.info(f"Found {len(files)} GOES files")
        return files
    
    def get_latest(self, hours_back: int = 1) -> Optional[GOESFile]:
        """Get the most recent GOES file.
        
        Args:
            hours_back: How far back to search for recent files
        
        Returns:
            Most recent GOESFile or None if not found
        """
        # First try with latest available date from bucket
        latest_available = self._get_latest_available_date()
        
        if latest_available:
            # Search around the latest available date
            end_time = latest_available
            start_time = end_time - timedelta(hours=hours_back)
        else:
            # Fall back to current time
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=hours_back)
        
        files = self.list_available(start_time=start_time, end_time=end_time, max_files=10)
        
        return files[0] if files else None
    
    def download_file(self, goes_file: GOESFile, output_dir: Path) -> Path:
        """Download a single GOES file.

        Uses a .tmp staging file so that a failed or interrupted download never
        leaves a partial .nc on disk that would pass the exists() cache check.

        Args:
            goes_file: GOESFile to download
            output_dir: Local directory to save to

        Returns:
            Path to downloaded file
        """
        self._refresh_fs_if_stale()

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        local_path = output_dir / goes_file.filename

        if local_path.exists():
            logger.debug(f"File already exists: {local_path}")
            goes_file.local_path = local_path
            return local_path

        tmp_path = local_path.with_suffix(f'.tmp.{os.getpid()}.{threading.get_ident()}')
        try:
            logger.info(f"Downloading {goes_file.filename}...")
            self.fs.get(goes_file.s3_path, str(tmp_path))
            if local_path.exists():
                tmp_path.unlink(missing_ok=True)
            else:
                tmp_path.rename(local_path)
            goes_file.local_path = local_path
            logger.info(f"Downloaded: {local_path}")
            return local_path
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            logger.error(f"Failed to download {goes_file.s3_path}: {e}")
            raise
    
    def download_files(self, files: list[GOESFile], output_dir: Path) -> list[Path]:
        """Download multiple GOES files.
        
        Args:
            files: List of GOESFile objects
            output_dir: Local directory to save to
        
        Returns:
            List of paths to downloaded files
        """
        output_dir = Path(output_dir)
        downloaded = []
        
        for goes_file in files:
            try:
                path = self.download_file(goes_file, output_dir)
                downloaded.append(path)
            except Exception as e:
                logger.error(f"Failed to download {goes_file.filename}: {e}")
        
        return downloaded
    
    def open_dataset(self, goes_file: GOESFile) -> xr.Dataset:
        """Open a GOES file as xarray Dataset directly from S3 or local.
        
        Args:
            goes_file: GOESFile to open
        
        Returns:
            xarray Dataset
        """
        if goes_file.local_path and goes_file.local_path.exists():
            return xr.open_dataset(goes_file.local_path)
        
        # Open directly from S3
        f = self.fs.open(goes_file.s3_path)
        return xr.open_dataset(f)


class GLMDownloader(GOESDownloader):
    """Specialized downloader for GLM lightning data.
    
    GLM data is much smaller than ABI (~1-10MB vs ~100-500MB)
    and has slightly different file structure.
    """
    
    def __init__(self, satellite: int = 16):
        """Initialize GLM downloader.
        
        Args:
            satellite: 16 (East) or 18 (West)
        """
        if satellite not in (16, 18, 19):
            raise ValueError("GLM only available on GOES-16, GOES-18, and GOES-19")
        
        super().__init__(satellite=satellite, domain="F", product="GLM-L2-LCFA")
        
        # GLM is Full Disk only
        logger.info(f"GLM Downloader initialized for GOES-{satellite}")
    
    def extract_lightning_points(self, dataset: xr.Dataset) -> dict:
        """Extract lightning flash data from GLM dataset.
        
        Returns:
            Dict with 'lat', 'lon', 'energy', 'time' arrays
        """
        try:
            # GLM variables vary by file version
            flash_lat = dataset.get('flash_lat', dataset.get('latitude'))
            flash_lon = dataset.get('flash_lon', dataset.get('longitude'))
            flash_energy = dataset.get('flash_energy', dataset.get('energy'))
            flash_time = dataset.get('flash_time_offset_of_first_event')
            
            if flash_lat is None or flash_lon is None:
                logger.warning("Could not find flash location variables in dataset")
                return {'lat': [], 'lon': [], 'energy': [], 'time': [], 'count': 0}
            
            # Filter valid flashes (remove NaN)
            valid = ~np.isnan(flash_lat) & ~np.isnan(flash_lon)
            
            return {
                'lat': flash_lat.values[valid],
                'lon': flash_lon.values[valid],
                'energy': flash_energy.values[valid] if flash_energy is not None else np.ones(valid.sum()),
                'time': flash_time.values[valid] if flash_time is not None else None,
                'count': int(valid.sum())
            }
        except Exception as e:
            logger.error(f"Error extracting lightning points: {e}")
            return {'lat': [], 'lon': [], 'energy': [], 'time': [], 'count': 0}
    
    def create_flash_geojson(self, dataset: xr.Dataset) -> dict:
        """Convert GLM lightning data to GeoJSON format for web display.
        
        Returns:
            GeoJSON FeatureCollection with Point features for each flash
        """
        data = self.extract_lightning_points(dataset)
        
        features = []
        for i in range(data['count']):
            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(data['lon'][i]), float(data['lat'][i])]
                },
                "properties": {
                    "energy": float(data['energy'][i]) if data['energy'] is not None else 1.0,
                    "index": i
                }
            }
            features.append(feature)
        
        return {
            "type": "FeatureCollection",
            "features": features,
            "count": data['count'],
            "satellite": f"GOES-{self.satellite}",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


# Convenience functions for common use cases

def download_latest_goes(
    satellite: int = 16,
    domain: str = "C",
    product: str = "ABI-L2-MCMIP",
    output_dir: str = "./satellite/goes/"
) -> Optional[Path]:
    """Download the most recent GOES file.
    
    Returns:
        Path to downloaded file or None if not found
    """
    downloader = GOESDownloader(satellite=satellite, domain=domain, product=product)
    latest = downloader.get_latest()
    
    if latest:
        return downloader.download_file(latest, Path(output_dir))
    return None


def download_latest_glm(satellite: int = 16, output_dir: str = "./satellite/glm/") -> Optional[Path]:
    """Download the most recent GLM lightning data.
    
    Returns:
        Path to downloaded file or None if not found
    """
    downloader = GLMDownloader(satellite=satellite)
    latest = downloader.get_latest()
    
    if latest:
        return downloader.download_file(latest, Path(output_dir))
    return None


def get_satellite_cache_dir(base_dir: Path, satellite: str, product: str) -> Path:
    """Get standardized cache directory for satellite data."""
    return base_dir / "satellite" / satellite.lower() / product.lower()
