import xmpp
from mmptypes import *
import utils
import time
import profile
import core
import glue
import pool
import config
import forms
import i18n
import traceback
import sys
import threading
import os
import asyncore
import Queue
import random
import socket

xmpp.NS_GATEWAY = 'jabber:iq:gateway'
xmpp.NS_STATS = 'http://jabber.org/protocol/stats'
xmpp.NS_ROSTERX = 'http://jabber.org/protocol/rosterx'

conf = config.Config()

class XMPPTransport:

	def __init__(self, name, disconame, server, port, password):
		self.name = name
		self.disconame = disconame
		self.port = port
		self.server = server
		self.password = password
		self.conn = xmpp.Component(self.name)
		self.pool = pool.MMPPool()
		self.zombie = Queue.Queue()
		self.full_stop = threading.Event()
		self.server_features = [
			xmpp.NS_DISCO_INFO,
			xmpp.NS_DISCO_ITEMS,
			xmpp.NS_STATS,
			xmpp.NS_VCARD,
			xmpp.NS_SEARCH,
			xmpp.NS_REGISTER,
			xmpp.NS_TIME,
			xmpp.NS_VERSION,
			xmpp.NS_LAST,
			xmpp.NS_GATEWAY
		]
		self.server_ids = {
			'category':'gateway',
			'type':'mrim',
			'name':self.disconame
		}

	def run(self):
		self.conn.connect((self.server,self.port))
		self.conn.auth(name=self.name, password=self.password)
		self.startup = time.time()

		self.conn.UnregisterDisconnectHandler(self.conn.DisconnectHandler)
		self.conn.RegisterDefaultHandler(lambda x,y: None)
		self.conn.RegisterHandler('iq', self.daemon_iq_handler)
		self.conn.RegisterHandler('presence', self.daemon_presence_handler)
		self.conn.RegisterHandler('message', self.daemon_message_handler)

		self.sform = forms.SearchForm().create()
		self.Features = forms.DiscoFeatures(self.server_ids,self.server_features).create()

		utils.start_daemon(self.pinger, (), 'pinger')
		#utils.start_daemon(self.billing, (), 'billing')
		utils.start_daemon(self.composing, (), 'composing')
		if conf.reconnect:
			utils.start_daemon(self.reanimator, (), 'reanimator')
		utils.start_daemon(self.asyncore_watcher, (), 'asyncore_watcher')

		self.conn.send_error = self.send_error
		if conf.probe:
			self.start_all_connections()

		while 1:
			if self.conn.isConnected():
				self.conn.Process(1)
			else:
				raise IOError("Lost connection to server")
				self.stop(notify=False)

	def daemon_iq_handler(self, conn, stanza):
		utils.start_daemon(self.iq_handler, (stanza,))

	def daemon_presence_handler(self, conn, stanza):
		utils.start_daemon(self.presence_handler, (stanza,))

	def daemon_message_handler(self, conn, stanza):
		utils.start_daemon(self.message_handler, (stanza,))

	def iq_handler(self, iq):
		ns = iq.getQueryNS()
		typ = iq.getType()
		if ns == xmpp.NS_REGISTER:
			self.iq_register_handler(iq)
		elif ns == xmpp.NS_GATEWAY:
			self.iq_gateway_handler(iq)
		elif ns == xmpp.NS_STATS:
			self.iq_stats_handler(iq)
		elif ns == xmpp.NS_TIME:
			self.iq_time_handler(iq)
		elif ns == xmpp.NS_LAST:
			self.iq_last_handler(iq)
		elif ns == xmpp.NS_SEARCH:
			self.iq_search_handler(iq)
		elif ns == xmpp.NS_VERSION:
			self.iq_version_handler(iq)
		elif iq.getTag('vCard') and iq.getTag('vCard').getNamespace()==xmpp.NS_VCARD:
			self.iq_vcard_handler(iq)
		elif ns == xmpp.NS_DISCO_INFO:
			self.iq_disco_info_handler(iq)
		elif ns == xmpp.NS_DISCO_ITEMS:
			self.iq_disco_items_handler(iq)
		elif (ns not in self.server_features):
			self.send_not_implemented(iq)
		else:
			pass

	def presence_handler(self, presence):
		typ = presence.getType()
		if typ == 'unavailable':
			self.presence_unavailable_handler(presence)
		elif typ == 'subscribe':
			self.presence_subscribe_handler(presence)
		elif typ == 'subscribed':
			self.presence_subscribed_handler(presence)
		elif typ == 'unsubscribe':
			self.presence_unsubscribe_handler(presence)
		elif typ == 'unsubscribed':
			self.presence_unsubscribed_handler(presence)
		elif typ == 'probe':
			pass
		elif typ == 'error':
			self.presence_error_handler(presence)
		elif not typ:
			self.presence_available_handler(presence)

	def message_handler(self, message):
		jid_to = message.getTo()
		jid_to_stripped = jid_to.getStripped()
		if jid_to_stripped == self.name:
			self.message_server_handler(message)
		else:
			self.message_user_handler(message)

	def iq_disco_info_handler(self, iq):
		jid_from = iq.getFrom()
		jid_from_stripped = jid_from.getStripped()
		jid_to = iq.getTo()
		jid_to_stripped = jid_to.getStripped()
		typ = iq.getType()
		node = iq.getTagAttr('query','node')
		if jid_to_stripped==self.name and typ=='get' and not node:
			reply = iq.buildReply(typ='result')
			reply.setQueryPayload(self.Features)
			self.conn.send(reply)
		elif jid_to_stripped==self.name and typ=='get' and node:
			if jid_from_stripped not in conf.admins:
				self.send_not_implemented(iq)
				return
			count = len(self.pool.getJids())
			ids = {
				'category':'directory',
				'type':'user',
				'name':'online (%s)' % count
			} 
			features = [
				xmpp.NS_DISCO_INFO,
				xmpp.NS_DISCO_ITEMS
			]
			reply = iq.buildReply(typ='result')
			reply.setQueryPayload(forms.DiscoFeatures(ids,features).create())
			reply.setTagAttr('query','node','online')
			self.conn.send(reply)
		elif jid_to_stripped==self.name and typ=='result':
			rosterx_id = iq.getID()
			mmp_conn = self.pool.get(jid_from)
			if mmp_conn and mmp_conn._got_roster:
				try:
					mmp_conn.ids.pop(mmp_conn.ids.index(rosterx_id))
				except (ValueError,IndexError):
					return
				iq_children = iq.getQueryChildren()
				features = [c.getAttr('var') for c in iq_children if c.getName()=='feature']
				if xmpp.NS_ROSTERX in features:
					self.roster_exchange(jid_from)
		else:
			self.send_not_implemented(iq)

	def iq_disco_items_handler(self, iq):
		jid_from = iq.getFrom()
		jid_from_stripped = jid_from.getStripped()
		jid_to = iq.getTo()
		jid_to_stripped = jid_to.getStripped()
		typ = iq.getType()
		node = iq.getTagAttr('query','node')
		if jid_to_stripped==self.name and typ=='get' and node:
			items = []
			reply = iq.buildReply(typ='result')
			if jid_from_stripped in conf.admins:
				for jid in self.pool.getJids():
					try:
						legacy_user = self.pool.get(jid).user
						item_attrs = {
							'jid':jid,
							'name':legacy_user,
						}
						item = xmpp.Node('item',attrs=item_attrs)
						items.append(item)
					except:
						pass
				reply.setQueryPayload(items)
				reply.setTagAttr('query','node','online')
			else:
				reply.setQueryPayload([])
			self.conn.send(reply)
		elif jid_to_stripped==self.name and typ=='get' and not node:
			reply = iq.buildReply(typ='result')
			if jid_from_stripped in conf.admins:
				item = xmpp.Node('item',attrs={
							'jid':self.name,
							'name':'online',
							'node':'online'
						}
				)
				reply.setQueryPayload([item])
			else:
				reply.setQueryPayload([])
			self.conn.send(reply)
		else:
			self.send_not_implemented(iq)

	def iq_register_handler(self, iq):
		jid_from = iq.getFrom()
		jid_from_stripped = jid_from.getStripped()
		jid_to = iq.getTo()
		jid_to_stripped = jid_to.getStripped()
		typ = iq.getType()
		iq_children = iq.getQueryChildren()
		if (typ=='get') and (jid_to_stripped==self.name) and (not iq_children):
			repl = iq.buildReply(typ='result')
			repl.setQueryPayload(self.get_register_form(jid_from_stripped))
			self.conn.send(repl)
		elif typ == 'set' and (jid_to_stripped==self.name) and iq_children:
			query_tag = iq.getTag('query')
			if query_tag.getTag('email') and query_tag.getTag('password'):
				user = query_tag.getTagData('email')
				password = query_tag.getTagData('password')
				error = xmpp.ERR_BAD_REQUEST
				if not user:
					text = i18n.NULL_EMAIL
					self.send_error(iq,error,text)
					return
				if not password:
					text = i18n.NULL_PASSWORD
					self.send_error(iq,error,text)
					return
				if '@' not in user:
					text = i18n.INCORRECT_EMAIL
					self.send_error(iq,error,text)
					return
				#account = profile.Profile(jid_from_stripped)
				#account.setUsername(user)
				#account.setPassword(password)
				mmp_conn = self.pool.get(jid_from)
				if mmp_conn:
					mmp_conn.exit()
				self.mrim_connection_start(jid_from, None, iq)
			elif query_tag.getTag('remove'):
				account = profile.Profile(jid_from_stripped)
				if account.remove():
					ok_iq = iq.buildReply(typ='result')
					ok_iq.setPayload([],add=0)
					self.conn.send(ok_iq)
					unsub = xmpp.Presence(to=jid_from_stripped,frm=self.name)
					unsub.setType('unsubscribe')
					self.conn.send(unsub)
					unsub.setType('unsubscribed')
					self.conn.send(unsub)
				else:
					pass
				mmp_conn = self.pool.get(jid_from)
				if mmp_conn:
					mmp_conn.exit()
			else:
				self.send_bad_request(iq)
		else:
			self.send_not_implemented(iq)

	def iq_gateway_handler(self, iq):
		jid_to = iq.getTo()
		jid_to_stripped = jid_to.getStripped()
		iq_children = iq.getQueryChildren()
		typ = iq.getType()
		if (typ=='get') and (jid_to_stripped==self.name) and (not iq_children):
			repl = iq.buildReply(typ='result')
			query = xmpp.Node('query', attrs={'xmlns':xmpp.NS_GATEWAY})
			query.setTagData('desc', i18n.ENTER_EMAIL)
			query.setTag('prompt')
			repl.setPayload([query])
			self.conn.send(repl)
		elif (typ=='set') and (jid_to_stripped==self.name) and iq_children:
			e_mail = [node.getData() for node in iq_children if node.getName()=='prompt']
			if len(e_mail) == 1:
				prompt = xmpp.simplexml.Node('prompt')
				prompt.setData(utils.mail2jid(e_mail[0]))
				repl = iq.buildReply(typ='result')
				repl.setQueryPayload([prompt])
				self.conn.send(repl)
			else:
				self.send_bad_request(iq)
		else:
			self.send_not_implemented(iq)

	def iq_search_handler(self, iq):
		jid_from = iq.getFrom()
		jid_to = iq.getTo()
		jid_to_stripped = jid_to.getStripped()
		typ = iq.getType()
		iq_children = iq.getQueryChildren()
		if (typ=='get') and (jid_to_stripped==self.name):
			if iq_children:
				self.send_bad_request(iq)
			else:
				mmp_conn = self.pool.get(jid_from)
				if mmp_conn:
					repl = iq.buildReply(typ='result')
					repl.setQueryPayload([self.sform])
					self.conn.send(repl)
				else:
					err = xmpp.ERR_REGISTRATION_REQUIRED
					txt = i18n.NOT_CONNECTED
					self.send_error(iq, err, txt)
		elif typ=='set' and (jid_to_stripped==self.name):
			if not iq_children:
				self.send_bad_request(iq)
			else:
				proto_dict = self.workup_search_input(iq)
				mmp_conn = self.pool.get(jid_from)
				if mmp_conn:
					mmp_conn.search(proto_dict,iq)
				else:
					err = xmpp.ERR_REGISTRATION_REQUIRED
					txt = i18n.NOT_CONNECTED
					self.send_error(iq, err, txt)
		else:
			self.send_not_implemented(iq)

	def iq_version_handler(self, iq):
		jid_to = iq.getTo()
		jid_to_stripped = jid_to.getStripped()
		typ = iq.getType()
		iq_children = iq.getQueryChildren()
		if typ=='get' and (jid_to_stripped==self.name):
			if iq_children:
				self.send_bad_request(iq)
			else:
				repl = iq.buildReply(typ='result')
				query = repl.getTag('query')
				query.setTagData('name', conf.program)
				query.setTagData('version', conf.version)
				query.setTagData('os', conf.os)
				self.conn.send(repl)
		else:
			self.send_not_implemented(iq)

	def iq_time_handler(self, iq):
		jid_to = iq.getTo()
		jid_to_stripped = jid_to.getStripped()
		typ = iq.getType()
		iq_children = iq.getQueryChildren()
		if typ=='get' and (jid_to_stripped==self.name):
			if iq_children:
				self.send_bad_request(iq)
			else:
				repl = iq.buildReply(typ='result')
				query = xmpp.Node('query',attrs={'xmlns':xmpp.NS_TIME})
				T = utils.gettime()
				query.setTagData('utc', T['utc'])
				query.setTagData('tz', T['tz'])
				query.setTagData('display', T['display'])
				repl.setPayload([query])
				self.conn.send(repl)
		else:
			self.send_not_implemented(iq)

	def iq_last_handler(self, iq):
		jid_to = iq.getTo()
		jid_to_stripped = jid_to.getStripped()
		typ = iq.getType()
		iq_children = iq.getQueryChildren()
		if (jid_to_stripped==self.name) and typ=='get':
			if iq_children:
				self.send_bad_request(iq)
			else:
				repl = iq.buildReply(typ='result')
				repl.getTag('query').setAttr('seconds', int(time.time()-self.startup))
				self.conn.send(repl)
		else:
			self.send_not_implemented(iq)

	def iq_vcard_handler(self, iq):
		jid_from = iq.getFrom()
		jid_to = iq.getTo()
		jid_from_stripped = jid_from.getStripped()
		jid_to_stripped = jid_to.getStripped()
		typ = iq.getType()
		if jid_to_stripped!=self.name and typ=='get':
			if self.pool.get(jid_from):
				e_mail = utils.jid2mail(jid_to_stripped)
				self.pool.get(jid_from).get_vcard(e_mail, iq)
			else:
				err = xmpp.ERR_REGISTRATION_REQUIRED
				txt = i18n.NOT_CONNECTED
				self.send_error(iq,err,txt)
		elif jid_to_stripped==self.name and typ=='get':
			vcard = xmpp.Node('vCard', attrs={'xmlns':xmpp.NS_VCARD})
			vcard.setTagData('NICKNAME', conf.program)
			vcard.setTagData('DESC', 'XMPP to Mail.Ru-IM Transport')
			vcard.setTagData('URL', 'http://svn.xmpp.ru/repos/mrim')
			repl = iq.buildReply(typ='result')
			repl.setPayload([vcard])
			self.conn.send(repl)
		else:
			self.send_not_implemented(iq)

	def iq_stats_handler(self, iq):
		jid_to_stripped = iq.getTo()
		typ = iq.getType()
		iq_children = iq.getQueryChildren()
		if jid_to_stripped==self.name and typ=='get':
			payload = []
			if not iq_children:
				total = xmpp.Node('stat', attrs={'name':'users/total'})
				online = xmpp.Node('stat', attrs={'name':'users/online'})
				payload = [total,online]
			else:
				for n in [child for child in iq_children if child.getName()=='stat']:
					if n.getAttr('name') == 'users/online':
						stat = xmpp.Node('stat', attrs={'units':'users'})
						stat.setAttr('name','users/online')
						stat.setAttr('value',len(self.pool.connections.keys()))
						payload.append(stat)
					elif n.getAttr('name') == 'users/total':
						stat = xmpp.Node('stat', attrs={'units':'users'})
						stat.setAttr('name','users/total')
						stat.setAttr('value',len(os.listdir(conf.profile_dir)))
						payload.append(stat)
					else:
						s = xmpp.Node('stat', attrs={'name':n.getAttr('name')})
						err = xmpp.Node('error', attrs={'code':'404'})
						err.setData('Not Found')
						s = xmpp.Node('stat', attrs={'name':n.getAttr('name')})
						s.addChild(node=err)
						payload.append(s)
			if not payload:
				pass
			else:
				iq_repl = iq.buildReply(typ='result')
				iq_repl.setQueryPayload(payload)
				self.conn.send(iq_repl)

	def presence_available_handler(self, presence):
		jid_from = presence.getFrom()
		jid_from_stripped = jid_from.getStripped()
		jid_to = presence.getTo()
		jid_to_stripped = jid_to.getStripped()
		show = presence.getShow()
		if jid_to_stripped!=self.name:
			return
		mmp_conn = self.pool.get(jid_from)
		if mmp_conn:
			self.show_status(jid_from, show, mmp_conn)
		else:
			self.mrim_connection_start(jid_from, show)

	def presence_unavailable_handler(self, presence):
		jid_from = presence.getFrom()
		jid_from_stripped = jid_from.getStripped()
		jid_to = presence.getTo()
		jid_to_stripped = jid_to.getStripped()
		if jid_to_stripped!=self.name:
			return
		mmp_conn = self.pool.get(jid_from)
		offline = xmpp.Presence(to=jid_from, frm=self.name,typ='unavailable')
		if mmp_conn:
			if [jid_from.getResource()] != self.pool.getResources(jid_from):
				self.conn.send(offline)
				mmp_conn.broadcast_offline(jid_from)
				self.pool.pop(jid_from)
			else:
				mmp_conn.exit()
		else:
			self.conn.send(offline)

	def presence_subscribe_handler(self, presence):
		'''To be completely rewritten'''
		jid_from = presence.getFrom()
		jid_from_stripped = jid_from.getStripped()
		jid_to = presence.getTo()
		jid_to_stripped = jid_to.getStripped()
		if jid_to_stripped==self.name:
			self.conn.send(xmpp.Presence(frm=self.name,to=jid_from_stripped,typ='subscribed'))
			self.conn.send(xmpp.Presence(frm=self.name,to=jid_from))
		else:
			e_mail = utils.jid2mail(jid_to_stripped)
			mmp_conn = self.pool.get(jid_from)
			if not mmp_conn:
				return
			if (e_mail in mmp_conn.contact_list.getEmails()) and \
			   (not mmp_conn.contact_list.getAuthFlag(e_mail)) \
			   and (not mmp_conn.contact_list.getUserFlags(e_mail)):
				subd = xmpp.Presence(frm=jid_to_stripped,to=jid_from_stripped,typ='subscribed')
				self.conn.send(subd)
				pres = xmpp.Presence(frm=jid_to_stripped,to=jid_from)
				status = mmp_conn.contact_list.getUserStatus(e_mail)
				if status == STATUS_AWAY:
					pres.setShow('away')
					self.conn.send(pres)
				elif status == STATUS_ONLINE:
					self.conn.send(pres)
			else:
				mmp_conn.add_contact(e_mail)

	def presence_subscribed_handler(self, presence):
		jid_from = presence.getFrom()
		jid_from_stripped = jid_from.getStripped()
		jid_to = presence.getTo()
		jid_to_stripped = jid_to.getStripped()
		if jid_to_stripped==self.name:
			pass
		else:
			e_mail = utils.jid2mail(jid_to_stripped)
			mmp_conn = self.pool.get(jid_from)
			if mmp_conn:
				mmp_conn.mmp_send_subscribed(e_mail)

	def presence_unsubscribe_handler(self, presence):
		jid_from = presence.getFrom()
		jid_to = presence.getTo()
		jid_from_stripped = jid_from.getStripped()
		jid_to_stripped = jid_to.getStripped()
		mmp_conn = self.pool.get(jid_from)
		if jid_to_stripped==self.name:
			if mmp_conn:
				mmp_conn.exit()
			unsub = xmpp.Presence(to=jid_from_stripped,frm=self.name)
			unsub.setType('unsubscribe')
			self.conn.send(unsub)
		elif mmp_conn and mmp_conn._got_roster:
			e_mail = utils.jid2mail(jid_to_stripped)
			mmp_conn.del_contact(e_mail)

	def presence_unsubscribed_handler(self, presence):
		jid_from = presence.getFrom()
		jid_to = presence.getTo()
		jid_from_stripped = jid_from.getStripped()
		jid_to_stripped = jid_to.getStripped()
		mmp_conn = self.pool.get(jid_from)
		if jid_to_stripped==self.name:
			if mmp_conn:
				mmp_conn.exit()
			unsub = xmpp.Presence(to=jid_from_stripped,frm=self.name)
			unsub.setType('unsubscribed')
			self.conn.send(unsub)
			account = profile.Profile(jid_from_stripped)
			account.remove()

	def presence_error_handler(self, presence):
		pass

	def message_server_handler(self, message):
		pass

	def message_user_handler(self, message):
		jid_from = message.getFrom()
		jid_to = message.getTo()
		jid_to_stripped = jid_to.getStripped()
		mail_to = utils.jid2mail(jid_to_stripped)
		body = message.getBody()
		x = message.getTag('x')
		mmp_conn = self.pool.get(jid_from)
		if not (mmp_conn and mmp_conn._is_authorized):
			if body:
				err = xmpp.ERR_REGISTRATION_REQUIRED
				txt = i18n.NOT_CONNECTED
				self.send_error(message, err, txt)
		elif body:
			mmp_conn.send_message(mail_to,body,message)
			try:
				mmp_conn.typing_users.pop(mail_to)
			except KeyError:
				pass

		elif x and x.getNamespace()=='jabber:x:event':
			if x.getTag('composing') and x.getTag('id'):
				mmp_conn.mmp_send_typing_notify(mail_to)
				mmp_conn.typing_users[mail_to] = time.time()
			elif x.getTag('id'):
				try:
					mmp_conn.typing_users.pop(mail_to)
				except KeyError:
					pass

	def get_register_form(self, jid):
		user = profile.Profile(jid).getUsername()
		instr = xmpp.Node('instructions')
		instr.setData(i18n.ENTER_EMAIL_AND_PASSWORD)
		email = xmpp.Node('email')
		passwd = xmpp.Node('password')
		if user:
			reg = xmpp.Node('registered')
			email.setData(user)
			return [instr,reg,email,passwd]
		else:
			return [instr,email,passwd]

	def workup_search_input(self,mess):
		xdf = [i for i in mess.getQueryChildren() if i.getNamespace() == xmpp.NS_DATA]
		d = {}
		if not xdf:
			return d
		xdata = xmpp.protocol.DataForm(node=xdf[0])
		for k,v in xdata.asDict().items():
			if not v:
				continue
			value = utils.str2win(v.strip())
			if not value:
				continue
			if k == 'email':
				d = {}
				user = domain = ''
				try:
					user, domain = value.split('@')
				except ValueError:
					pass
				if user and domain:
					d[MRIM_CS_WP_REQUEST_PARAM_USER] = user
					d[MRIM_CS_WP_REQUEST_PARAM_DOMAIN] = domain
				return d
			elif k == 'nick':
				d[MRIM_CS_WP_REQUEST_PARAM_NICKNAME] = value
			elif k == 'firstname':
				d[MRIM_CS_WP_REQUEST_PARAM_FIRSTNAME] = value
			elif k == 'lastname':
				d[MRIM_CS_WP_REQUEST_PARAM_LASTNAME] = value
			elif k == 'sex':
				d[MRIM_CS_WP_REQUEST_PARAM_SEX] = value
			elif k == 'age_from':
				d[MRIM_CS_WP_REQUEST_PARAM_DATE1] = value
			elif k == 'age_to':
				d[MRIM_CS_WP_REQUEST_PARAM_DATE2] = value
			elif k == 'city_id':
				d[MRIM_CS_WP_REQUEST_PARAM_CITY_ID] = value
			elif k == 'country_id':
				d[MRIM_CS_WP_REQUEST_PARAM_COUNTRY_ID] = value
			elif k == 'zodiac':
				d[MRIM_CS_WP_REQUEST_PARAM_ZODIAC] = value
			elif k == 'birthmonth':
				d[MRIM_CS_WP_REQUEST_PARAM_BIRTHDAY_MONTH] = value
			elif k == 'birthday':
				d[MRIM_CS_WP_REQUEST_PARAM_BIRTHDAY_DAY] = value
			elif k == 'online' and value == '1':
				d[MRIM_CS_WP_REQUEST_PARAM_ONLINE] = ' '
		return d

	def roster_exchange(self, jid_from):
		mmp_conn = self.pool.get(jid_from)
		if not (mmp_conn and mmp_conn._got_roster):
			return
		rosterx = xmpp.Iq(frm=self.name,to=jid_from,typ='set')
		x = xmpp.Node('x', attrs={'xmlns':xmpp.NS_ROSTERX})
		items = []
		for e_mail in mmp_conn.contact_list.getEmails():
			if not mmp_conn.contact_list.getUserFlags(e_mail):
				jid = utils.mail2jid(e_mail)
				name = mmp_conn.contact_list.getUserNick(e_mail)
				item = xmpp.Node('item',attrs={'action':'add','jid':jid,'name':name})
				items.append(item)
		if items:
			x.setPayload(items)
			rosterx.addChild(node=x)
			self.conn.send(rosterx)

	def show_status(self, jid, show, mmp_conn=None):
		resource = xmpp.JID(jid).getResource()
		if mmp_conn:
			if mmp_conn._is_authorized:
				status = utils.show2status(show)
				mmp_conn.mmp_change_status(status)
				if resource not in self.pool.getResources(jid):
					self.conn.send(xmpp.Presence(frm=self.name,to=jid))
					mmp_conn.broadcast_online(jid)
		else:
			pass
		if resource:
			self.pool.push(jid)

	def mrim_connection_start(self, jid, init_status, iq_register=None):
		if not self.pool.lock(jid):
			self.show_status(jid, init_status)
			return
		if iq_register:
			user = iq_register.getTag('query').getTagData('email')
			password = iq_register.getTag('query').getTagData('password')
		else:
			account = profile.Profile(xmpp.JID(jid).getStripped())
			user = account.getUsername()
			password = account.getPassword()
			if not (user and password):
				self.pool.unlock(jid)
				return
		glue.MMPConnection(user,password,self.conn,
			jid,init_status,self.pool,self.zombie,iq_register)

	def send_not_implemented(self, iq):
		if iq.getType() in ['set','get']:
			self.send_error(iq)

	def send_bad_request(self, iq):
		if iq.getType() in ['set','get']:
			self.send_error(iq,xmpp.ERR_BAD_REQUEST)

	def send_error(self, stanza, error=xmpp.ERR_FEATURE_NOT_IMPLEMENTED, text='', reply=1):
		e = xmpp.Error(stanza,error,reply)
		if text:
			e.setTagData('error', text)
			e.getTag('error').setTagData('text', text)
			e.getTag('error').getTag('text').setAttr('xml:lang','ru-RU')
		self.conn.send(e)

	def send_probe(self, jid):
		probe = xmpp.Presence(frm=self.name,to=jid,typ='probe')
		self.conn.send(probe)

	def stop(self, notify=True):
		for mmp_conn in self.pool.getConnections():
			mmp_conn.exit(notify)
		self.zombie.put(None)
		self.full_stop.set()

	def start_all_connections(self):
		probe = xmpp.Presence(frm=conf.name,typ='probe')
		users = [f[:f.find('.xdb')] for f in os.listdir(conf.profile_dir) if f.endswith('.xdb')]
		for user in users:
			probe.setTo(user)
			self.conn.send(probe)

	def asyncore_loop(self):
		asyncore.loop(1)

	def asyncore_watcher(self):
		while not self.full_stop.isSet():
			time.sleep(1)
			current_threads = [tred.getName() for tred in threading.enumerate()]
			if 'asyncore_loop' not in current_threads:
				utils.start_daemon(self.asyncore_loop, (), 'asyncore_loop')

	def pinger(self):
		sleep_secs = 5
		while not self.full_stop.isSet():
			for connection in self.pool.getConnections():
				ping_period = connection.ping_period
				last_ping_time = connection.last_ping_time
				if (time.time() - last_ping_time) > (ping_period - sleep_secs):
					connection.ping()
			time.sleep(sleep_secs)

	def composing(self):
		sleep_secs = 5
		while not self.full_stop.isSet():
			for connection in self.pool.getConnections():
				typing_users = connection.typing_users
				for u,t in typing_users.items():
					if (time.time()-t)>sleep_secs:
						connection.mmp_send_typing_notify(u)
			time.sleep(sleep_secs)

	def billing(self):
		while not self.full_stop.isSet():
			time.sleep(5)
			for i in self.pool.getJids():
				print i.__str__(), ":", self.pool.resources[i]
			print [tred.getName() for tred in threading.enumerate()]

	def reanimator(self):
		probe = xmpp.Presence(frm=conf.name,typ='probe')
		while 1:
			dead_jid = self.zombie.get()
			if dead_jid:
				probe.setTo(xmpp.JID(dead_jid).getStripped())
				self.conn.send(probe)
			else:
				break