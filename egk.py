#!/usr/bin/python2

from helpers import *
from exc import *
from smartcard.Exceptions import *
from smartcard.System import readers
from datetime import datetime
import zlib
from xml.dom.minidom import parseString

VALID_ATRs = [
	[0x3B, 0xDD, 0x97, 0xFF, 0x81, 0xB1, 0xFE, 0x45, 0x1F, 0x03, 0x00, 0x64,
	 0x04, 0x05, 0x08, 0x03, 0x73, 0x96, 0x21, 0xD0, 0x00, 0x90, 0x00, 0xC8],
]

# define the APDUs used in this script
SELECT_HCA  = [0x00, 0xA4, 0x04, 0x0C, 0x06, 0xD2, 0x76, 0x00, 0x00, 0x01, 0x02]
SELECT_ROOT = [0x00, 0xA4, 0x04, 0x0C, 0x07, 0xD2, 0x76, 0x00, 0x01, 0x44, 0x80, 0x00]

FILE_PD = [0x00, 0xA4, 0x02, 0x0C, 0x02, 0xD0, 0x01]
FILE_VD = [0x00, 0xA4, 0x02, 0x0C, 0x02, 0xD0, 0x02]

def ADPU_READ(pos, length):
	bpos = [pos >> 8 & 0xFF, pos & 0xFF]
	return [0x00, 0xB0, bpos[0], bpos[1], length]

READ_EF_STATUS_VD = [0x00, 0xB0, 0x8C, 0x00, 0x19]

READ_EF_VERSION_1 = [0x00, 0xB2, 0x01, 0x84, 0x00]
READ_EF_VERSION_2 = [0x00, 0xB2, 0x02, 0x84, 0x00]
READ_EF_VERSION_3 = [0x00, 0xB2, 0x03, 0x84, 0x00]

# get all the available readers
r = readers()
print "Available readers:", r

reader = r[0]
print "Using:", reader

try:
	connection = reader.createConnection()
	connection.connect()

	def run(adpu, expect_result=(0x90, 0x00)):
		data, sw1, sw2 = connection.transmit(adpu)
		if expect_result:
			if not (sw1, sw2) == expect_result:
				raise AssertionError('Got (%02X, %02X) - ' % (sw1, sw2) + 'expected (%02X, %02X)' % expect_result)
		return data

	def get_version(adpu):
		data, sw1, sw2 = connection.transmit(adpu)
		assert (sw1, sw2) == (0x90, 0x00)
		hdata = unpack_bcd(data)
		version = "%i.%i.%i" % (
			decode_bcd(hdata[0:3]),
			decode_bcd(hdata[3:6]),
			decode_bcd(hdata[6:10])
		)
		return version

	def get_file(offset, length):
		data = []
		pointer = offset
		while (pointer - offset) < length:
			readlen = pd_len - (pointer - offset)
			readlen = pd_len if length < 0xFC else 0xFC
			data_chunk = run( ADPU_READ(pointer, readlen) )
			pointer += readlen
			data.extend(data_chunk)
		return data

	atr = connection.getATR()
	if not atr in VALID_ATRs: raise InvalidCardException(atr)

	run(SELECT_ROOT) # Root Directory
	
	version_1 = get_version(READ_EF_VERSION_1)
	version_2 = get_version(READ_EF_VERSION_2)
	version_3 = get_version(READ_EF_VERSION_3)

	run(SELECT_HCA) # HealthCareApplication Directory

	# EF.STATUS: transaction_open, last_data_update, xsd_version
	data = run(READ_EF_STATUS_VD)
	transaction_open = bool(int(chr(data[0])))
	last_data_update = datetime.strptime("".join([chr(c) for c in data[1:15]]), "%Y%m%d%H%M%S")
	hdata = unpack_bcd(data[15:20])
	version_xsd = "%i.%i.%i" % (
		decode_bcd(hdata[0:3]),
		decode_bcd(hdata[3:6]),
		decode_bcd(hdata[6:10])
	)

	if version_2 == '3.0.1' and version_3 == '3.0.3':
		generation = 'G1plus'
	else:
		generation = 'G1'

	run(FILE_PD) # open PersonalData FILE

	# read PersonalData size
	data = run( ADPU_READ(0x00, 0x02) )
	pd_len = data[0] * 16 * 16 + data[1]
	pd_len -= 0x02 # pd_len includes own length

	# read PersonalData
	pd_gz = get_file(0x02, pd_len)
	pd_gz.extend([0x00] * 16) # make zlib happy - screw seamingly truncated streams
	pd_gz = bytearray(pd_gz)
	pd_gz = bytes(pd_gz)
	pd_xml = zlib.decompress(pd_gz, 15+16)

	run(FILE_VD) # open VersicherungsDaten

	# read VersicherungsDaten header
	data = run( ADPU_READ(0x00, 0x08) )
	vd_start = data[0] * 16 * 16 + data[1]
	vd_end   = data[2] * 16 * 16 + data[3]
	vd_len   = vd_end - (vd_start - 1) # -1 because we count from 0 on

	# read VersicherungsDaten
	vd_gz = get_file(vd_start, vd_len)
	vd_gz.extend([0x00] * 16) # make zlib happy - screw seamingly truncated streams...
	vd_gz = bytearray(vd_gz)
	vd_gz = bytes(vd_gz)
	vd_xml = zlib.decompress(vd_gz, 15+16)

	# parse xml
	pd = parseString(pd_xml)
	personal_data = {
		'versichertennummer': str(pd.getElementsByTagName('vsdp:Versicherten_ID')[0].childNodes[0].data),
		'birthdate': datetime.strptime(pd.getElementsByTagName('vsdp:Geburtsdatum')[0].childNodes[0].data, '%Y%m%d').date().__str__(),
		'firstname': pd.getElementsByTagName('vsdp:Vorname')[0].childNodes[0].data,
		'lastname': pd.getElementsByTagName('vsdp:Nachname')[0].childNodes[0].data,
		'gender': pd.getElementsByTagName('vsdp:Geschlecht')[0].childNodes[0].data,
		'address': pd.getElementsByTagName('vsdp:Strasse')[0].childNodes[0].data,
		'zip': str(pd.getElementsByTagName('vsdp:Postleitzahl')[0].childNodes[0].data),
		'city': pd.getElementsByTagName('vsdp:Ort')[0].childNodes[0].data,
		'country_code': str(pd.getElementsByTagName('vsdp:Wohnsitzlaendercode')[0].childNodes[0].data)
	}
	# Optional:
	# Vorsatzwort: von, de, van, da, del, zu
	# Namenszusatz: Graf, Freiherr, Freifrau 
	# Titel: Dr., Prof., Dipl.

	vd = parseString(vd_xml)
	for element in vd.getElementsByTagName('vsda:Kostentraegerkennung'):
		if element.parentNode.tagName == 'vsda:Kostentraeger':
			versicherungsnummer = str(element.childNodes[0].data)
	for element in vd.getElementsByTagName('vsda:Name'):
		if element.parentNode.tagName == 'vsda:Kostentraeger':
			versicherungsname = str(element.childNodes[0].data)
	versicherungs_data = {
		'versicherungsnummer': versicherungsnummer,
		'versicherungsname': versicherungsname,
		'beginn': datetime.strptime(vd.getElementsByTagName('vsda:Beginn')[0].childNodes[0].data, '%Y%m%d').date().__str__()
	}

	print """
	atr: %(atr)s
	version_1: %(version_1)s
	version_2: %(version_2)s
	version_3: %(version_3)s
	version_xsd: %(version_xsd)s
	generation: %(generation)s
	last_data_update: %(last_data_update)s
	transaction_open: %(transaction_open)s
	personal_data: %(personal_data)s
	versicherungs_data: %(versicherungs_data)s
	""" % {
		'atr': ' '.join('%02X' % byte for byte in atr),
		'version_1': version_1,
		'version_2': version_2,
		'version_3': version_3,
		'version_xsd': version_xsd,
		'generation': generation,
		'last_data_update': last_data_update,
		'transaction_open': transaction_open,
		'personal_data': personal_data,
		'versicherungs_data': versicherungs_data
	}

	connection.disconnect()

except (NoCardException, CardConnectionException):
	print '%s: no card inserted' % reader
