import struct
from crcmod.predefined import PredefinedCrc
from serial import Serial

class Packet:
	def __init__(self, data):
		self.data = data
	
	def gen_data(self):
		return self.data
	
	def __bytes__(self):
		data = self.gen_data()
		crc = PredefinedCrc('crc-16-mcrf4xx')
		crc.update(data)
		digest = crc.digest()
		return data + bytes([digest[1], digest[0]])
	
	def __repr__(self):
		return '<{}: {}>'.format(type(self).__name__, tohex(self.gen_data()))

	def decode(self):
		data = self.gen_data()
		if len(data) == 2:
			(addr,) = struct.unpack('<H', data)
			return SyncReply(addr & 0x7FFF, 1 if addr & 0x8000 else 0)
		elif len(data) == 3:
			addr, n = struct.unpack('<HB', data)
			return SyncPing(addr & 0x7FFF, 1 if addr & 0x8000 else 0, n)
		elif len(data) >= 7:
			dest, src, n, size = struct.unpack('<HHBH', data[:7])
			if size + 5 == len(data):
				flags = 0
				if dest & 0x8000:
					dest &= 0x7FFF
					flags |= 1
				if src & 0x8000:
					src &= 0x7FFF
					flags |= 2
				return Message(dest, src, data[7:], flags, n)
			else:
				return self
		else:
			return self

class SyncPing(Packet):
	def __init__(self, addr, flag, counter):
		self.addr = addr
		self.flag = flag
		self.counter = counter
	
	def __repr__(self):
		return '<({}*) PING->{:04X}: {:02X}>'.format('1' if self.flag else '0', self.addr, self.counter)
	
	def gen_data(self):
		return struct.pack('<HB', self.addr | (0x8000 if self.flag else 0), self.counter)

class SyncReply(Packet):
	def __init__(self, addr, flag=False):
		self.addr = addr
		self.flag = flag
	
	def __repr__(self):
		return '<(*{}) {:04X}->PONG>'.format('1' if self.flag else '0', self.addr)
	
	def gen_data(self):
		return struct.pack('<H', self.addr | (0x8000 if self.flag else 0))

class Message(Packet):
	def __init__(self, dest, src, payload, flags=0, n=4):
		self.dest = dest
		self.src = src
		if type(payload) is tuple:
			cmd, data = payload
			self.payload = struct.pack('<H', cmd) + data
		else:
			self.payload = payload
		self.flags = flags
		self.n = n
	
	def __repr__(self):
		return '<({:02b}) {:04X}->{:04X}: {}>'.format(self.flags, self.src, self.dest, tohex(self.payload))
	
	def gen_data(self):
		dest_field = (self.dest | 0x8000) if (self.flags & 1) else self.dest
		src_field = (self.src | 0x8000) if (self.flags & 2) else self.src
		return struct.pack('<HHBH', dest_field, src_field, self.n, len(self.payload)+2) + self.payload
	
	def getcmd(self):
		return struct.unpack('<H', self.payload[:2])[0]
	
	def getarg(self):
		return self.payload[2:]

class Interface:
	def __init__(self, port, **kwargs):
		if 'baudrate' in kwargs:
			baudrate = kwargs['baudrate']
		else:
			baudrate = 38400

		self.serial = Serial(port=port, baudrate=baudrate)
	
	def read(self):
		while True:
			while self.serial.read() != b'\x1e': pass
			data = b''
			found_non_1e = False
			while True:
				b = self.serial.read()
				if b == b'\x1e':
					if found_non_1e:
						break
				else:
					found_non_1e = True
					data += b
			
			data = data.replace(b'\x7d\x3e', b'\x1e').replace(b'\x7d\x5d', b'\x7d')
			check = data[-2:]
			data = data[:-2]

			#crc = PredefinedCrc('crc-16-mcrf4xx')
			#crc.update(data)
			#digest = crc.digest()
			
			#if check[0] == digest[1] and check[1] == digest[0]:
			return Packet(data).decode()
			#else:
			#	print('crc fail: ' + tohex(data + check))
	
	def write(self, packet):
		self.serial.write(b'\x1e' + bytes(packet).replace(b'\x7d', b'\x7d\x5d').replace(b'\x1e', b'\x7d\x3e'))

class SyncState:
	def __init__(self, myaddr, master=False, slave=False):
		self.flags = 0
		self.myaddr = myaddr
		if master:
			self.flags |= 2
		if slave:
			self.flags |= 1
	
	def receive(self, pkt):
		if type(pkt) is SyncPing:
			if pkt.addr == self.myaddr:
				if pkt.flag:
					if not self.flags & 1:
						print('setting flag by ping')
					self.flags |= 1
				else:
					if self.flags & 1:
						print('clearing flag by ping')
					self.flags &= 2
			return True
		elif type(pkt) is SyncReply:
			if pkt.addr == self.myaddr:
				if pkt.flag:
					self.flags |= 2
				else:
					self.flags &= 1
			return True
		elif type(pkt) is Message:
			accept = True
			if pkt.src == self.myaddr:
				accept = (self.flags ^ pkt.flags == 1)
			elif pkt.dest == self.myaddr:
				accept = (self.flags ^ pkt.flags == 2)
			if accept:
				self.flags = pkt.flags
			return accept
	
	def next(self, as_master):
		self.flags ^= (2 if as_master else 1)
		return self.flags
	
	def reply(self):
		return SyncReply(self.myaddr, self.flags & 2)

class Intellibus:
	def __init__(self, iface, **kwargs):
		self.counter = 1
		self.syncs = {}
		if 'debug' in kwargs:
			dbg = kwargs['debug']
			if type(dbg) is dict:
				self.debug = dbg
			elif type(dbg) is tuple or type(dbg) is list:
				self.debug = dict([(k,None) for k in dbg])
			elif type(dbg) is str:
				self.debug = {}
				for item in dbg.split(','):
					kv = item.split('=')
					if len(kv) > 1:
						self.debug[kv[0]] = kv[1]
					else:
						self.debug[kv[0]] = None
			else:
				raise TypeError('debug must be a dict, tuple/list, or a string in the format "key[=value],key2[=value2],..."')
		if type(iface) is str:
			self.bus = Interface(iface)
		else:
			self.bus = iface
		self.listeners = []
	
	def sync(self, addr):
		if addr not in self.syncs:
			self.syncs[addr] = SyncState(addr)
		return self.syncs[addr]
	
	def send_raw(self, pkt):
		if 'tx' in self.debug:
			if 'sync' in self.debug or type(pkt) not in (SyncPing, SyncReply):
				print('TX: {}'.format(pkt))
		self.bus.write(pkt)
	
	def send(self, dest, src, msg, **kwargs):
		if type(msg) is Message:
			msg = msg.payload
		if 'flags' in kwargs:
			flags = kwargs['flags']
		else:
			flags = self.sync(dest if src == 0 else src).next(src == 0)
		pkt = Message(dest, src, msg, flags)
		for _ in range(kwargs['count'] if 'count' in kwargs else 6):
			self.send_raw(pkt)
	
	def run(self):
		self.stop_flag = False
		while not self.stop_flag:
			pkt = self.bus.read()
			isSynced = True
			doDebugOutput = 'sync' in self.debug and 'rx' in self.debug
			if type(pkt) is SyncPing:
				self.sync(pkt.addr).receive(pkt)
				self.counter = pkt.counter % 0x7F + 1
			elif type(pkt) is SyncReply:
				self.sync(pkt.addr).receive(pkt)
			elif type(pkt) is Message:
				if 'rx' in self.debug:
					doDebugOutput = doDebugOutput or (int(self.debug['rx']) in (pkt.src, pkt.dest))
				if pkt.dest == 0x7FFF:
					pass
				elif 0x7001 <= pkt.dest <= 0x707F:
					if pkt.dest & 0xFF == self.counter:
						self.counter = self.counter % 0x7F + 1
					else:
						isSynced = False
				elif pkt.dest == 0:
					isSynced = self.sync(pkt.src).receive(pkt)
				else:
					isSynced = self.sync(pkt.dest).receive(pkt)
			else:
				doDebugOutput = True

			for l in self.listeners:
				l.receive(pkt, isSynced)

			if doDebugOutput:
				print('RX: {}'.format(pkt))
	
	def stop(self):
		self.stop_flag = True
	
	def broadcast(self, msg, **kwargs):
		if type(msg) is Message:
			msg = msg.payload
		pkt = Message(0x7000+self.counter, 0, msg, 0)
		self.counter = self.counter % 0x7F + 1
		for _ in range(kwargs['count'] if 'count' in kwargs else 1):
			self.send_raw(pkt)
	
	def reg_listener(self, listener):
		self.listeners.append(listener)
	
	def sync_reply(self, addr):
		self.send_raw(self.sync(addr).reply())

class VirtDevice:
	def __init__(self, ibus:Intellibus, kind:int, model:int, serial_no:bytes, hdw_conf:int=0, fw_ver:(int,int)=0):
		self.addr = 0
		self.ibus = ibus
		self.kind = kind
		self.model = model
		self.serial_no = serial_no.rjust(6, b'\0')
		self.hdw_conf = hdw_conf
		self.fw_ver = bytes(fw_ver)
		ibus.listeners.append(self)
	
	def receive(self, pkt, synced):
		if type(pkt) is SyncPing:
			if pkt.addr == self.addr:
				self.ibus.sync_reply(pkt.addr)
		elif type(pkt) is Message:
			cmd = pkt.getcmd()
			arg = pkt.getarg()
			if cmd == 0xBBC and self.addr == 0:
				self.ibus.send(0, self.addr, (0xBB9, self.serial_no + struct.pack('<HHHH', 0x100, self.model, self.kind, self.hdw_conf) + self.fw_ver), count=3)
			elif cmd == 0xBBA:
				if arg[:6] == self.serial_no:
					self.addr = struct.unpack('<H', arg[-2:])[0]
					self.ibus.send(0, self.addr, (0xBBB, arg), count=3)
			elif pkt.dest == self.addr:
				self.handle_cmd(cmd, arg)

	def handle_cmd(cmd, arg):
		pass

def tohex(data):
	return ' '.join(['{:02X}'.format(b) for b in data])

def fromhex(text):
	text = text.replace(' ', '')
	if len(text) % 2 == 0:
		split = [text[i:i+2] for i in range(0, len(text), 2)]
		return bytes([int(h, 16) for h in split])
	else:
		raise ValueError('An even number of hex digits must be given.')
