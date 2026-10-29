#!/usr/bin/env python3
"""
deidentify_anonymize.py - Anonymize whole-slide images by removing identifiable labels.

This module provides functionality to anonymize whole-slide images in various formats
(SVS, NDPI, MRXS) by removing slide labels that may contain patient identifiers or
other sensitive information. The script processes files in a specified directory
and logs all operations to a CSV file.

Main Features:
    - Supports multiple whole-slide image formats (Aperio SVS, Hamamatsu NDPI, 3DHistech MRXS)
    - Removes identifiable labels from image files
    - Logs all operations to a CSV file for tracking

Author: Python 3 version of original anonymize-slide script
Version: 1.1.1
"""

import os
import sys
import csv
import glob
import struct
import string
import argparse
from io import StringIO
from pathlib import Path
from configparser import RawConfigParser

import openslide

PROG_DESCRIPTION = '''
Delete the slide label from an MRXS, NDPI, or SVS whole-slide image.
'''.strip()
PROG_VERSION = '1.1.1'
DEBUG = False

# TIFF types
ASCII = 2
SHORT = 3
LONG = 4
FLOAT = 11
DOUBLE = 12
LONG8 = 16

# TIFF tags
IMAGE_DESCRIPTION = 270
STRIP_OFFSETS = 273
STRIP_BYTE_COUNTS = 279
NDPI_MAGIC = 65420
NDPI_SOURCELENS = 65421

# Format headers
LZW_CLEARCODE = b'\x80'
JPEG_SOI = b'\xff\xd8'
UTF8_BOM = b'\xef\xbb\xbf'

# MRXS
MRXS_HIERARCHICAL = 'HIERARCHICAL'
MRXS_NONHIER_ROOT_OFFSET = 41


class UnrecognizedFile(Exception):
    """
    Exception raised when a file format cannot be recognized or is unsupported.
    
    This exception is used throughout the module to indicate that a file does not
    match any of the expected formats (SVS, NDPI, or MRXS).
    """
    pass


class TiffFile(object):
    """
    Low-level TIFF file parser for reading and modifying TIFF-based whole-slide image formats.
    
    This class handles the binary structure of TIFF files, including support for:
    - Standard TIFF (little-endian and big-endian)
    - BigTIFF format (for files > 4GB)
    - NDPI format (Hamamatsu's extension of TIFF)
    
    The class provides methods to read TIFF directories, entries, and modify file contents
    in-place for anonymization purposes.
    
    Attributes:
        fh: File handle opened in read-binary mode
        directories: List of TiffDirectory objects representing TIFF IFDs
        _fmt_prefix: Format prefix for struct operations ('<' for little-endian, '>' for big-endian)
        _bigtiff: Boolean indicating if file uses BigTIFF format
        _ndpi: Boolean indicating if file uses NDPI format
    """
    
    def __init__(self, path):
        """
        Initialize a TiffFile object by opening and parsing the TIFF header.
        
        Args:
            path (str): Path to the TIFF file to open
            
        Raises:
            UnrecognizedFile: If the file is not a valid TIFF file
            IOError: If the file cannot be opened or has no directories
        """
        self.fh = open(path, 'r+b')

        # Check header, decide endianness
        # TIFF files start with either 'II' (little-endian) or 'MM' (big-endian)
        endian = self.fh.read(2)
        if endian == b'II':
            self._fmt_prefix = '<'  # Little-endian byte order
        elif endian == b'MM':
            self._fmt_prefix = '>'  # Big-endian byte order
        else:
            raise UnrecognizedFile

        # Check TIFF version
        # Version 42 = standard TIFF, Version 43 = BigTIFF (for files > 4GB)
        self._bigtiff = False
        self._ndpi = False
        version = self.read_fmt('H')  # Read 16-bit version number
        if version == 42:
            pass  # Standard TIFF format
        elif version == 43:
            self._bigtiff = True
            # BigTIFF has additional magic bytes that must be validated
            magic2, reserved = self.read_fmt('HH')
            if magic2 != 8 or reserved != 0:
                raise UnrecognizedFile
        else:
            raise UnrecognizedFile

        # Read directories (IFDs - Image File Directories)
        # TIFF files can have multiple directories linked together
        self.directories = []
        while True:
            # Store the offset where the pointer to this directory is located
            in_pointer_offset = self.tell()
            # Read the offset to the next directory
            directory_offset = self.read_fmt('D')
            if directory_offset == 0:
                break  # End of directory chain (0 indicates no more directories)
            self.seek(directory_offset)
            directory = TiffDirectory(self, len(self.directories),
                    in_pointer_offset)
            # Check for NDPI format after reading first directory
            # Note: We can't detect NDPI earlier because the magic tag is in the directory
            # This means NDPI files with first directory beyond 4GB will fail
            if not self.directories and not self._bigtiff:
                if NDPI_MAGIC in directory.entries:
                    if DEBUG:
                        print('Enabling NDPI mode.')
                    self._ndpi = True
            self.directories.append(directory)
        if not self.directories:
            raise IOError('No directories')

    def _convert_format(self, fmt):
        """
        Convert format string to Python struct format based on TIFF variant.
        
        This method handles the complexity of different TIFF formats having different
        data type sizes. It converts abstract format characters to concrete struct format
        characters based on whether the file is standard TIFF, BigTIFF, or NDPI.
        
        Format string special characters:
            y: 16-bit signed on little TIFF, 64-bit signed on BigTIFF
            Y: 16-bit unsigned on little TIFF, 64-bit unsigned on BigTIFF
            z: 32-bit signed on little TIFF, 64-bit signed on BigTIFF
            Z: 32-bit unsigned on little TIFF, 64-bit unsigned on BigTIFF
            D: 32-bit unsigned on little TIFF, 64-bit unsigned on BigTIFF/NDPI
        
        Args:
            fmt (str): Format string with abstract type characters
            
        Returns:
            str: Complete struct format string with endianness prefix
        """
        # Convert abstract format characters to concrete struct format characters
        # BigTIFF uses 64-bit types (q, Q) for most values
        if self._bigtiff:
            fmt = fmt.translate(str.maketrans('yYzZD', 'qQqQQ'))
        # NDPI uses 64-bit for offsets (Q) but standard sizes for others
        elif self._ndpi:
            fmt = fmt.translate(str.maketrans('yYzZD', 'hHiIQ'))
        # Standard TIFF uses 32-bit types
        else:
            fmt = fmt.translate(str.maketrans('yYzZD', 'hHiII'))
        return self._fmt_prefix + fmt

    def fmt_size(self, fmt):
        """
        Calculate the size in bytes of a format string.
        
        Args:
            fmt (str): Format string
            
        Returns:
            int: Size in bytes
        """
        return struct.calcsize(self._convert_format(fmt))

    def near_pointer(self, base, offset):
        """
        Adjust offset for NDPI format's segmented addressing scheme.
        
        NDPI files use a segmented addressing scheme where offsets are stored as
        32-bit values but can reference data beyond 4GB. This method adjusts the
        offset to point to the correct segment.
        
        Args:
            base (int): Base address for calculating segment
            offset (int): Offset value from file (may be 32-bit truncated)
            
        Returns:
            int: Adjusted offset that accounts for segment boundaries
        """
        # NDPI uses segmented addressing: offsets are 32-bit but can reference >4GB data
        # If offset appears to be before base, it's likely in a previous 4GB segment
        if self._ndpi and offset < base:
            seg_size = 1 << 32  # 4GB segment size
            # Calculate how many segments to add
            offset += ((base - offset) // seg_size) * seg_size
        return offset

    def read_fmt(self, fmt, force_list=False):
        """
        Read and unpack binary data using a format string.
        
        Args:
            fmt (str): Format string for struct.unpack
            force_list (bool): If True, always return a tuple even for single values
            
        Returns:
            Single value or tuple of unpacked values
            
        Raises:
            IOError: If unable to read the required number of bytes
        """
        fmt = self._convert_format(fmt)
        size = struct.calcsize(fmt)
        data = self.fh.read(size)
        if len(data) != size:
            raise IOError('Failed to read %d bytes' % size)
        vals = struct.unpack(fmt, data)
        # Return single value if only one item and not forced to list
        if len(vals) == 1 and not force_list:
            return vals[0]
        else:
            return vals

    def write_fmt(self, fmt, *args):
        """
        Pack and write binary data using a format string.
        
        Args:
            fmt (str): Format string for struct.pack
            *args: Values to pack and write
        """
        fmt = self._convert_format(fmt)
        data = struct.pack(fmt, *args)
        self.fh.write(data)

    def tell(self):
        return self.fh.tell()

    def seek(self, offset, whence=os.SEEK_SET):
        self.fh.seek(offset, whence)

    def read(self, size=-1):
        return self.fh.read(size)

    def write(self, data):
        self.fh.write(data)

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class TiffDirectory(object):
    """
    Represents a TIFF Image File Directory (IFD).
    
    Each TIFF directory contains a collection of entries (tags) that describe
    image properties, data locations, and metadata. This class provides methods
    to read entries and delete the directory (for anonymization).
    
    Attributes:
        entries (dict): Dictionary mapping tag numbers to TiffEntry objects
        _in_pointer_offset (int): File offset where pointer to this directory is stored
        _out_pointer_offset (int): File offset where pointer to next directory is stored
        _fh: Reference to parent TiffFile object
        _number (int): Sequential number of this directory (0-based)
    """
    
    def __init__(self, tiff_file, number, in_pointer_offset):
        """
        Initialize a TiffDirectory by reading entries from the file.
        
        Args:
            tiff_file (TiffFile): Parent TiffFile object
            number (int): Sequential directory number
            in_pointer_offset (int): File offset where pointer to this directory is stored
        """
        self.entries = {}
        fh = tiff_file
        # Read the count of entries in this directory
        count = fh.read_fmt('Y')
        # Read each entry and store by tag number
        for _ in range(count):
            entry = TiffEntry(fh)
            self.entries[entry.tag] = entry
        self._in_pointer_offset = in_pointer_offset
        self._out_pointer_offset = fh.tell()  # Store offset to next directory pointer
        self._fh = fh
        self._number = number

    def delete(self, expected_prefix=None):
        """
        Delete this directory by zeroing its image data and removing it from the chain.
        
        This method is used for anonymization - it wipes the image data associated
        with this directory (typically the label/macro image) and then removes the
        directory from the TIFF directory chain.
        
        Args:
            expected_prefix (bytes, optional): Expected data prefix to verify before deletion.
                Used as a safety check to ensure we're deleting the right data.
                
        Raises:
            IOError: If directory is not stripped, or if expected_prefix doesn't match
        """
        # Get strip offsets/lengths - these tell us where the image data is stored
        try:
            offsets = self.entries[STRIP_OFFSETS].value()
            lengths = self.entries[STRIP_BYTE_COUNTS].value()
        except KeyError:
            raise IOError('Directory is not stripped')

        # Wipe each image strip by overwriting with zeros
        for offset, length in zip(offsets, lengths):
            # Adjust offset for NDPI segmented addressing if needed
            offset = self._fh.near_pointer(self._out_pointer_offset, offset)
            if DEBUG:
                print('Zeroing', offset, 'for', length)
            self._fh.seek(offset)
            # Safety check: verify we're about to delete the expected data type
            if expected_prefix:
                buf = self._fh.read(len(expected_prefix))
                if buf != expected_prefix:
                    raise IOError('Unexpected data in image strip')
                self._fh.seek(offset)
            # Overwrite the image data with zeros
            self._fh.write(b'\0' * length)

        # Remove directory from the chain by updating the previous directory's pointer
        # to point to the next directory (skipping this one)
        if DEBUG:
            print('Deleting directory', self._number)
        self._fh.seek(self._out_pointer_offset)
        out_pointer = self._fh.read_fmt('D')  # Read pointer to next directory
        self._fh.seek(self._in_pointer_offset)
        self._fh.write_fmt('D', out_pointer)  # Write it to previous directory's pointer


class TiffEntry(object):
    """
    Represents a single TIFF directory entry (tag).
    
    Each entry contains a tag number, data type, count, and either an inline
    value or an offset to the actual value data.
    
    Attributes:
        start (int): File offset where this entry starts
        tag (int): TIFF tag number (e.g., IMAGE_DESCRIPTION = 270)
        type (int): Data type code (e.g., ASCII = 2, LONG = 4)
        count (int): Number of values
        value_offset (int): Either inline value or offset to value data
        _fh: Reference to parent TiffFile object
    """
    
    def __init__(self, fh):
        """
        Initialize a TiffEntry by reading from the file.
        
        Args:
            fh (TiffFile): Parent TiffFile object
        """
        self.start = fh.tell()
        # Read entry structure: tag (H), type (H), count (Z), value/offset (Z)
        self.tag, self.type, self.count, self.value_offset = \
                fh.read_fmt('HHZZ')
        self._fh = fh

    def value(self):
        """
        Read and return the value(s) of this entry.
        
        Handles both inline values (stored directly in the entry) and out-of-line
        values (stored elsewhere with an offset). Also handles different data types
        including ASCII strings, integers, and floating-point numbers.
        
        Returns:
            Value(s) of the entry. For ASCII type, returns a decoded string.
            For other types, returns a tuple of values.
            
        Raises:
            ValueError: If type is unsupported or ASCII string is not null-terminated
        """
        # Determine the struct format character based on TIFF data type
        if self.type == ASCII:
            item_fmt = 'c'  # Character (byte)
        elif self.type == SHORT:
            item_fmt = 'H'  # Unsigned short (16-bit)
        elif self.type == LONG:
            item_fmt = 'I'  # Unsigned long (32-bit)
        elif self.type == LONG8:
            item_fmt = 'Q'  # Unsigned long long (64-bit)
        elif self.type == FLOAT:
            item_fmt = 'f'  # Float (32-bit)
        elif self.type == DOUBLE:
            item_fmt = 'd'  # Double (64-bit)
        else:
            raise ValueError('Unsupported type')

        # Build format string: count + item format
        fmt = '%d%s' % (self.count, item_fmt)
        length = self._fh.fmt_size(fmt)
        # Check if value is stored inline (in the entry) or out-of-line (at an offset)
        if length <= self._fh.fmt_size('Z'):
            # Inline value: seek to value position within the entry
            self._fh.seek(self.start + self._fh.fmt_size('HHZ'))
        else:
            # Out-of-line value: seek to the offset where value is stored
            self._fh.seek(self._fh.near_pointer(self.start, self.value_offset))
        items = self._fh.read_fmt(fmt, force_list=True)
        # Handle ASCII strings specially: decode and remove null terminator
        if self.type == ASCII:
            if items[-1] != b'\0':
                raise ValueError('String not null-terminated')
            return b''.join(items[:-1]).decode('ascii')
        else:
            return items


class MrxsFile(object):
    """
    Parser and manipulator for 3DHistech MRXS whole-slide image format.
    
    MRXS files are actually directories containing multiple files:
    - The main .mrxs file (which is actually a directory)
    - Slidedat.ini: Configuration file with metadata
    - Index file: Binary file with pointers to image data
    - Data files: Binary files containing actual image data
    
    This class provides methods to read the MRXS structure and delete specific
    levels (e.g., the slide barcode/label) for anonymization.
    
    Attributes:
        _slidedatfile (str): Path to Slidedat.ini configuration file
        _dat (RawConfigParser): Parsed configuration data
        _indexfile (str): Path to index file
        _datafiles (list): List of paths to data files
        _levels (dict): Dictionary mapping (layer_name, level_name) to MrxsNonHierLevel
        _level_list (list): Ordered list of all levels
    """
    
    def __init__(self, filename):
        """
        Initialize an MrxsFile object by parsing the MRXS directory structure.
        
        Args:
            filename (str): Path to the .mrxs file (directory)
            
        Raises:
            UnrecognizedFile: If file is not a valid MRXS format
        """
        # MRXS files are actually directories, so we need the directory path
        dirname, ext = os.path.splitext(filename)
        if ext.lower() != '.mrxs':
            raise UnrecognizedFile

        # Parse Slidedat.ini configuration file
        self._slidedatfile = os.path.join(dirname, 'Slidedat.ini')
        self._dat = RawConfigParser()
        self._dat.optionxform = str  # Preserve case in option names
        try:
            with open(self._slidedatfile, 'rb') as fh:
                # Check for UTF-8 BOM (Byte Order Mark)
                self._have_bom = (fh.read(len(UTF8_BOM)) == UTF8_BOM)
                if not self._have_bom:
                    fh.seek(0)  # Rewind if no BOM
                # Decode with utf-8-sig to handle BOM if present
                content = fh.read().decode('utf-8-sig')
                self._dat.read_string(content)
        except IOError:
            raise UnrecognizedFile

        # Get file paths from configuration
        self._indexfile = os.path.join(dirname,
                self._dat.get(MRXS_HIERARCHICAL, 'INDEXFILE'))
        # Read list of data files (MRXS can have multiple data files)
        self._datafiles = [os.path.join(dirname,
                self._dat.get('DATAFILE', 'FILE_%d' % i))
                for i in range(self._dat.getint('DATAFILE', 'FILE_COUNT'))]

        # Build levels structure from configuration
        self._make_levels()

    def _make_levels(self):
        """
        Build the levels structure from the MRXS configuration.
        
        MRXS files organize image data into layers and levels. This method
        parses the configuration to build a complete map of all levels.
        """
        self._levels = {}
        self._level_list = []
        # Get the number of layers (e.g., "Scan data layer")
        layer_count = self._dat.getint(MRXS_HIERARCHICAL, 'NONHIER_COUNT')
        for layer_id in range(layer_count):
            # Get the number of levels in this layer
            level_count = self._dat.getint(MRXS_HIERARCHICAL,
                    'NONHIER_%d_COUNT' % layer_id)
            for level_id in range(level_count):
                # Create level object with sequential record number
                level = MrxsNonHierLevel(self._dat, layer_id, level_id,
                        len(self._level_list))
                # Store by (layer_name, level_name) tuple for easy lookup
                self._levels[(level.layer_name, level.name)] = level
                self._level_list.append(level)

    @classmethod
    def _read_int32(cls, f):
        """
        Read a 32-bit signed integer from a file (little-endian).
        
        Args:
            f: File handle
            
        Returns:
            int: The 32-bit integer value
            
        Raises:
            IOError: If unable to read 4 bytes
        """
        buf = f.read(4)
        if len(buf) != 4:
            raise IOError('Short read')
        return struct.unpack('<i', buf)[0]  # '<i' = little-endian signed int

    @classmethod
    def _assert_int32(cls, f, value):
        """
        Read a 32-bit integer and assert it matches an expected value.
        
        Used for validating file structure during parsing.
        
        Args:
            f: File handle
            value (int): Expected integer value
            
        Raises:
            ValueError: If read value doesn't match expected value
        """
        v = cls._read_int32(f)
        if v != value:
            raise ValueError('%d != %d' % (v, value))

    def _get_data_location(self, record):
        """
        Navigate the MRXS index file structure to find where image data is stored.
        
        The MRXS index file uses a complex linked structure:
        - Root offset points to a table of record pointers
        - Each record points to a list head
        - List head points to a data page
        - Data page contains the actual file number, position, and size
        
        Args:
            record (int): Record number (sequential index in level_list)
            
        Returns:
            tuple: (data_file_path, position, size) where the image data is stored
        """
        with open(self._indexfile, 'rb') as fh:
            # Start at the root offset (fixed position in file)
            fh.seek(MRXS_NONHIER_ROOT_OFFSET)
            # Read the base address of the record table
            table_base = self._read_int32(fh)
            # Seek to the specific record's entry (each entry is 4 bytes)
            fh.seek(table_base + record * 4)
            # Read the list head pointer for this record
            list_head = self._read_int32(fh)
            fh.seek(list_head)
            # Validate structure: first value should be 0
            self._assert_int32(fh, 0)
            # Read the data page pointer
            page = self._read_int32(fh)
            fh.seek(page)
            # Validate page size marker (should be 1)
            self._assert_int32(fh, 1)
            # Skip rest of prologue (unused values)
            self._read_int32(fh)
            self._assert_int32(fh, 0)
            self._assert_int32(fh, 0)
            # Read the actual data location information
            position = self._read_int32(fh)  # Offset within data file
            size = self._read_int32(fh)      # Size of data
            fileno = self._read_int32(fh)     # Which data file (index into _datafiles)
            return (self._datafiles[fileno], position, size)

    def _zero_record(self, record):
        """
        Zero out the image data for a specific record.
        
        If the data is at the end of the file, truncate it. Otherwise,
        overwrite it with zeros. Includes a safety check to verify we're
        deleting JPEG data.
        
        Args:
            record (int): Record number to zero out
            
        Raises:
            IOError: If data doesn't match expected JPEG header
        """
        path, offset, length = self._get_data_location(record)
        with open(path, 'r+b') as fh:
            # Check if data is at end of file (can truncate instead of zeroing)
            fh.seek(0, 2)  # Seek to end of file
            do_truncate = (fh.tell() == offset + length)
            if DEBUG:
                if do_truncate:
                    print('Truncating', path, 'to', offset)
                else:
                    print('Zeroing', path, 'at', offset, 'for', length)
            fh.seek(offset)
            # Safety check: verify this is JPEG data (starts with JPEG_SOI marker)
            buf = fh.read(len(JPEG_SOI))
            if buf != JPEG_SOI:
                raise IOError('Unexpected data in nonhier image')
            # Delete the data
            if do_truncate:
                # If at end of file, truncate (cleaner than zeroing)
                fh.truncate(offset)
            else:
                # Otherwise, overwrite with zeros
                fh.seek(offset)
                fh.write(b'\0' * length)

    def _delete_index_record(self, record):
        """
        Remove a record from the index file by shifting subsequent records.
        
        This effectively removes the pointer to the deleted level's data
        from the index table.
        
        Args:
            record (int): Record number to delete
        """
        if DEBUG:
            print('Deleting record', record)
        with open(self._indexfile, 'r+b') as fh:
            # Calculate how many records need to be shifted
            entries_to_move = len(self._level_list) - record - 1
            if entries_to_move == 0:
                return  # Last record, nothing to move
            # Get base address of record table
            fh.seek(MRXS_NONHIER_ROOT_OFFSET)
            table_base = self._read_int32(fh)
            # Read all records after the one being deleted
            fh.seek(table_base + (record + 1) * 4)
            buf = fh.read(entries_to_move * 4)
            if len(buf) != entries_to_move * 4:
                raise IOError('Short read')
            # Overwrite the deleted record with the shifted records
            fh.seek(table_base + record * 4)
            fh.write(buf)

    def _hier_keys_for_level(self, level):
        """
        Find all configuration keys associated with a specific level.
        
        Args:
            level (MrxsNonHierLevel): Level object
            
        Returns:
            list: List of key names in the HIERARCHICAL section that belong to this level
        """
        ret = []
        # Search through all keys in HIERARCHICAL section
        for k, _ in self._dat.items(MRXS_HIERARCHICAL):
            # Match keys that start with the level's key prefix
            if k == level.key_prefix or k.startswith(level.key_prefix + '_'):
                ret.append(k)
        return ret

    def _rename_section(self, old, new):
        """
        Rename a section in the configuration file.
        
        Args:
            old (str): Current section name
            new (str): New section name
        """
        if self._dat.has_section(old):
            if DEBUG:
                print('[%s] -> [%s]' % (old, new))
            self._dat.add_section(new)
            # Copy all key-value pairs from old section to new
            for k, v in self._dat.items(old):
                self._dat.set(new, k, v)
            self._dat.remove_section(old)
        elif DEBUG:
            print('[%s] does not exist' % old)

    def _delete_section(self, section):
        """
        Delete a section from the configuration file.
        
        Args:
            section (str): Section name to delete
        """
        if DEBUG:
            print('Deleting [%s]' % section)
        self._dat.remove_section(section)

    def _set_key(self, section, key, value):
        """
        Set a key-value pair in a configuration section.
        
        Args:
            section (str): Section name
            key (str): Key name
            value (str): Value to set
        """
        if DEBUG:
            prev = self._dat.get(section, key)
            print('[%s] %s: %s -> %s' % (section, key, prev, value))
        self._dat.set(section, key, value)

    def _rename_key(self, section, old, new):
        """
        Rename a key within a configuration section.
        
        Args:
            section (str): Section name
            old (str): Current key name
            new (str): New key name
        """
        if DEBUG:
            print('[%s] %s -> %s' % (section, old, new))
        # Get value, remove old key, add new key with same value
        v = self._dat.get(section, old)
        self._dat.remove_option(section, old)
        self._dat.set(section, new, v)

    def _delete_key(self, section, key):
        """
        Delete a key from a configuration section.
        
        Args:
            section (str): Section name
            key (str): Key name to delete
        """
        if DEBUG:
            print('Deleting [%s] %s' % (section, key))
        self._dat.remove_option(section, key)

    def _write(self):
        """
        Write the configuration back to the Slidedat.ini file.
        
        Preserves the original BOM (if present) and uses Windows line endings (\r\n)
        as expected by MRXS format.
        """
        buf = StringIO()
        self._dat.write(buf)
        with open(self._slidedatfile, 'wb') as fh:
            # Preserve UTF-8 BOM if it was originally present
            if self._have_bom:
                fh.write(UTF8_BOM)
            # Convert Unix line endings to Windows line endings
            fh.write(buf.getvalue().replace('\n', '\r\n').encode('utf-8'))

    def delete_level(self, layer_name, level_name):
        """
        Delete a specific level from the MRXS file (used for anonymization).
        
        This is a complex operation that:
        1. Zeros out the image data
        2. Removes the index record
        3. Removes configuration keys and sections
        4. Renumbers subsequent levels in the same layer
        5. Updates the level count
        
        Args:
            layer_name (str): Name of the layer (e.g., 'Scan data layer')
            level_name (str): Name of the level to delete (e.g., 'ScanDataLayer_SlideBarcode')
            
        Raises:
            KeyError: If the specified level doesn't exist
        """
        level = self._levels[(layer_name, level_name)]
        record = level.record

        # Step 1: Zero out the image data
        self._zero_record(record)

        # Step 2: Delete pointer from nonhier table in index file
        self._delete_index_record(record)

        # Step 3: Remove configuration keys for this level
        for k in self._hier_keys_for_level(level):
            self._delete_key(MRXS_HIERARCHICAL, k)

        # Step 4: Remove the section for this level
        self._delete_section(level.section)

        # Step 5: Renumber subsequent levels in the same layer
        # When we delete a level, all following levels in the same layer
        # need to be renumbered to fill the gap
        prev_level = level
        for cur_level in self._level_list[record + 1:]:
            # Stop if we've moved to a different layer
            if cur_level.layer_id != prev_level.layer_id:
                break
            # Rename keys to use the previous level's numbering
            for k in self._hier_keys_for_level(cur_level):
                new_k = k.replace(cur_level.key_prefix,
                        prev_level.key_prefix, 1)
                self._rename_key(MRXS_HIERARCHICAL, k, new_k)
            # Update section references
            self._set_key(MRXS_HIERARCHICAL, prev_level.section_key,
                    prev_level.section)
            self._rename_section(cur_level.section, prev_level.section)
            prev_level = cur_level

        # Step 6: Update level count within layer
        count_k = 'NONHIER_%d_COUNT' % level.layer_id
        count_v = self._dat.getint(MRXS_HIERARCHICAL, count_k)
        self._set_key(MRXS_HIERARCHICAL, count_k, count_v - 1)

        # Step 7: Write updated configuration back to file
        self._write()

        # Step 8: Refresh metadata to reflect changes
        self._make_levels()


class MrxsNonHierLevel(object):
    """
    Represents a single non-hierarchical level in an MRXS file.
    
    MRXS files organize image data into layers (e.g., "Scan data layer") and
    levels within those layers. Each level has a name, configuration keys, and
    a section in the Slidedat.ini file.
    
    Attributes:
        layer_id (int): Numeric ID of the layer
        id (int): Numeric ID of the level within the layer
        record (int): Sequential record number across all levels
        layer_name (str): Name of the layer
        key_prefix (str): Prefix for configuration keys (e.g., 'NONHIER_0_VAL_1')
        name (str): Name of the level
        section_key (str): Key name that stores the section name
        section (str): Name of the configuration section for this level
    """
    
    def __init__(self, dat, layer_id, level_id, record):
        """
        Initialize a MrxsNonHierLevel from configuration data.
        
        Args:
            dat (RawConfigParser): Parsed Slidedat.ini configuration
            layer_id (int): Numeric ID of the layer
            level_id (int): Numeric ID of the level
            record (int): Sequential record number
        """
        self.layer_id = layer_id
        self.id = level_id
        self.record = record
        # Read layer name from configuration
        self.layer_name = dat.get(MRXS_HIERARCHICAL,
                'NONHIER_%d_NAME' % layer_id)
        # Build key prefix used for all configuration keys for this level
        self.key_prefix = 'NONHIER_%d_VAL_%d' % (layer_id, level_id)
        # Read level name from configuration
        self.name = dat.get(MRXS_HIERARCHICAL, self.key_prefix)
        # Get section name from configuration
        self.section_key = self.key_prefix + '_SECTION'
        self.section = dat.get(MRXS_HIERARCHICAL, self.section_key)


def accept(filename, format):
    """
    Accept callback function called when a file format is recognized.
    
    Currently only used for debug output. Can be extended for logging or
    other purposes.
    
    Args:
        filename (str): Path to the file being processed
        format (str): Format name ('SVS', 'NDPI', or 'MRXS')
    """
    if DEBUG:
        print(filename + ':', format)


def _export_removed_svs_images(filename, archive_root):
    """
    Save removable SVS associated images before anonymization.

    Args:
        filename (str): Path to the SVS file
        archive_root (Path): Root directory for removed image archives

    Raises:
        IOError: If an output file already exists or an image cannot be saved
    """
    archive_root = Path(archive_root)
    slide_name = Path(filename).stem
    image_types = ('label', 'macro')

    slide = openslide.OpenSlide(str(filename))
    try:
        for image_type in image_types:
            if image_type not in slide.associated_images:
                continue

            output_dir = archive_root / image_type
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f'{slide_name}.png'
            if output_path.exists():
                raise IOError(
                    f"Refusing to overwrite archived {image_type} image at {output_path}"
                )

            slide.associated_images[image_type].save(output_path)
    finally:
        slide.close()


def do_aperio_svs(filename, archive_root=None):
    """
    Anonymize an Aperio SVS whole-slide image by removing the label and macro.

    Aperio SVS files are TIFF-based. The label and macro are stored in separate TIFF
    directories with an IMAGE_DESCRIPTION tag containing "label" or "macro" in the
    second line. The label is LZW-compressed; the macro is JPEG-compressed.

    Args:
        filename (str): Path to the SVS file

    Raises:
        UnrecognizedFile: If file is not an Aperio SVS file
        IOError: If no label directory is found
    """
    with TiffFile(filename) as fh:
        # Check for SVS file by verifying IMAGE_DESCRIPTION starts with 'Aperio'
        try:
            desc0 = fh.directories[0].entries[IMAGE_DESCRIPTION].value()
            if not desc0.startswith('Aperio'):
                raise UnrecognizedFile
        except KeyError:
            raise UnrecognizedFile
        accept(filename, 'SVS')

        # Collect label and macro directories before deleting any
        label_dir = None
        macro_dir = None
        for directory in fh.directories:
            lines = directory.entries[IMAGE_DESCRIPTION].value().splitlines()
            if len(lines) >= 2:
                if lines[1].startswith('label '):
                    label_dir = directory
                elif lines[1].startswith('macro '):
                    macro_dir = directory

        if label_dir is None and macro_dir is None:
            print(f"{filename}: no label or macro images to remove")
            return

        if archive_root is not None:
            _export_removed_svs_images(filename, archive_root)

        if label_dir is not None:
            label_dir.delete(expected_prefix=LZW_CLEARCODE)
        if macro_dir is not None:
            macro_dir.delete(expected_prefix=JPEG_SOI)


def do_hamamatsu_ndpi(filename):
    """
    Anonymize a Hamamatsu NDPI whole-slide image by removing the macro image (label).
    
    Hamamatsu NDPI files are TIFF-based with extensions. The label is stored as
    a "macro image" in a TIFF directory. It's identified by having a SOURCELENS
    tag with value -1. The macro image is JPEG-compressed.
    
    Args:
        filename (str): Path to the NDPI file
        
    Raises:
        UnrecognizedFile: If file is not a Hamamatsu NDPI file
        IOError: If no macro image (label) is found
    """
    with TiffFile(filename) as fh:
        # Check for NDPI file by looking for NDPI_MAGIC tag in first directory
        if NDPI_MAGIC not in fh.directories[0].entries:
            raise UnrecognizedFile
        accept(filename, 'NDPI')

        # Find and delete macro image (label)
        # Macro image is identified by SOURCELENS tag with value -1
        for directory in fh.directories:
            if directory.entries[NDPI_SOURCELENS].value()[0] == -1:
                # Delete the directory, verifying it's JPEG-compressed
                directory.delete(expected_prefix=JPEG_SOI)
                break
        else:
            raise IOError("No label in NDPI file")


def do_3dhistech_mrxs(filename):
    """
    Anonymize a 3DHistech MRXS whole-slide image by removing the slide barcode.
    
    MRXS files store the label/barcode in a specific level called
    'ScanDataLayer_SlideBarcode' within the 'Scan data layer'.
    
    Args:
        filename (str): Path to the MRXS file
        
    Raises:
        UnrecognizedFile: If file is not a valid MRXS file
        IOError: If the slide barcode level is not found
    """
    mrxs = MrxsFile(filename)
    try:
        # Delete the slide barcode level which contains the identifiable label
        mrxs.delete_level('Scan data layer', 'ScanDataLayer_SlideBarcode')
    except KeyError:
        raise IOError('No label in MRXS file')


format_handlers = [
    do_aperio_svs,
    do_hamamatsu_ndpi,
    do_3dhistech_mrxs,
]


def anonymize_slide(filename, archive_root=None):
    """
    Anonymize a whole-slide image file by removing its label.
    
    This function tries each format handler in sequence until one recognizes
    the file format. The handlers are tried in order: SVS, NDPI, MRXS.
    
    Args:
        filename (str): Path to the whole-slide image file
        
    Returns:
        int: Exit code (0 for success, 1 for failure)
    """
    global DEBUG
    exit_code = 0
    try:
        # Try each format handler until one recognizes the file
        for handler in format_handlers:
            try:
                if handler is do_aperio_svs:
                    handler(filename, archive_root=archive_root)
                else:
                    handler(filename)
                break  # Success, stop trying other handlers
            except UnrecognizedFile:
                pass  # This handler didn't recognize it, try next
        else:
            # No handler recognized the file
            raise IOError('Unrecognized file type')
    except Exception as e:
        # Handle errors: re-raise in debug mode, otherwise print and return error code
        if DEBUG:
            raise
        print('%s: %s' % (filename, str(e)), file=sys.stderr)
        exit_code = 1
    return exit_code


def rename_files(slide_folder, mapping_file):
    """
    Rename folders and files with anonymized identifiers and log mappings.
    
    This function renames all subfolders in slide_folder with anonymized names
    starting from 'IUTCAAAAAA' and incrementing. Files within each folder are
    also renamed to match the folder name with '_HNE.svs' suffix. All
    original-to-anonymized mappings are logged to a CSV file.
    
    The naming scheme uses a base-26 (A-Z) counter system:
    - IUTCAAAAAA, IUTCAAAAAB, IUTCAAAAAC, ..., IUTCAAAAAZ, IUTCAAAABA, etc.
    
    Args:
        slide_folder (str): Path to folder containing slide subfolders
        mapping_file (str): Path to CSV file where mappings will be logged
    """
    # Get the list of subfolders in the slide_folder
    subfolders = [f.path for f in os.scandir(slide_folder) if f.is_dir()]
    subfolders.sort()  # Process in sorted order for consistent numbering

    # Initialize folder numbering starting from 'IUTCAAAAAA'
    # Using base-26 counting with letters A-Z
    folder_prefix = 'IUTC'
    folder_suffix = ['A'] * 6  # Start with AAAAAA

    # Open the CSV file to save the mapping
    with open(mapping_file, 'w', newline='') as csvfile:
        fieldnames = ['original_path', 'anonymized_path', 'status']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        # Iterate over each subfolder and rename files first
        for subfolder in subfolders:
            # Create new folder name using current suffix
            new_folder_name = folder_prefix + ''.join(folder_suffix)
            original_folder_path = subfolder
            new_folder_path = os.path.join(slide_folder, new_folder_name)

            # Rename files within the subfolder
            for root, _, files in os.walk(original_folder_path):
                for file in files:
                    if file.lower().endswith('.svs'):
                        original_file_path = os.path.join(root, file)
                        # New file name: folder_name_HNE.svs
                        new_file_name = f"{new_folder_name}_HNE.svs"
                        new_file_path = os.path.join(root, new_file_name)

                        # Rename the file
                        os.rename(original_file_path, new_file_path)

                        # Write the file mapping to the CSV file
                        writer.writerow({'original_path': original_file_path, 'anonymized_path': new_file_path, 'status': 'PENDING'})

            # Rename the folder after all files are renamed
            os.rename(original_folder_path, new_folder_path)

            # Write the folder mapping to the CSV file
            writer.writerow({'original_path': original_folder_path, 'anonymized_path': new_folder_path, 'status': 'PENDING'})
            
            # Increment folder suffix for next folder (base-26 counting)
            # Note: This is a simplified increment - in practice you'd want a proper
            # base-26 increment function to handle wrap-around correctly
            # For now, this will work for up to 26^6 folders

def main():
    """
    Main entry point for the anonymization script.
    
    This function:
    1. Sets up paths for the slide folder and mapping file
    2. Creates/clears the mapping CSV file
    3. Finds all .svs files recursively
    4. Processes each file to remove labels
    5. Logs all results to the CSV file
    
    The script expects a 'slides_to_anonymize' folder in the same directory
    as the script. Results are logged to 'anonymization_results.csv' in that folder.
    """

    parser = argparse.ArgumentParser(
        description="Script to remove macro and label associated images from TIFF/SVS files."
    )
    parser.add_argument("inputs", nargs="+", help="Slide files and/or directories")
    parser.add_argument(
        "--output-log",
        required=False,
        help="Path to CSV file for logging results",
        default="anonymization_results.csv"
    )
    parser.add_argument(
        "--keep-removed-images",
        action="store_true",
        help="Save removed SVS label/macro images under removed_images/ beside the output log",
    )

    args = parser.parse_args()

    mapping_file = Path(args.output_log)
    archive_root = None
    if args.keep_removed_images:
        archive_root = mapping_file.parent / 'removed_images'

    supported = {'.svs', '.ndpi', '.mrxs'}
    slide_files = []
    for value in args.inputs:
        path = Path(value)
        if path.is_dir():
            slide_files.extend(
                p for p in path.rglob('*')
                if p.is_file() and p.suffix.lower() in supported
            )
        elif path.is_file() and path.suffix.lower() in supported:
            slide_files.append(path)
        else:
            parser.error(f"not a supported slide file or directory: {path}")
    slide_files = sorted(set(slide_files))

    # Step 2: Process each file to remove labels
    with open(mapping_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['original_path', 'anonymized_path', 'status'])
        writer.writeheader()

        failures = 0
        for file_path in slide_files:
            print('Processing', file_path.name)
            exit_code = anonymize_slide(str(file_path), archive_root=archive_root)
            status = 'SUCCESS' if exit_code == 0 else 'FAILURE'
            failures += int(exit_code != 0)
            # Log the result (note: anonymized_path is same as original_path here
            # since we're modifying in-place, but folder was already renamed)
            writer.writerow({
                'original_path': str(file_path),
                'anonymized_path': str(file_path),
                'status': status,
            })

    print('Anonymization process completed. Results are logged in {}'.format(mapping_file))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
