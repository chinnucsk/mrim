#!/usr/bin/python

import transport
import config
import signal
import urllib2
import time
import sys
import traceback
import utils

conf = config.Config()

def main():
	while 1:
		try:
			xmpp_con = transport.XMPPTransport(conf.name,conf.disconame,
					conf.server,conf.port,conf.passwd)
			print "Connecting to XMPP server..."
			xmpp_con.run()
		except KeyboardInterrupt:
			xmpp_con.stop()
			sys.exit(0)
		except:
			traceback.print_exc()
			print "Connection to server lost"
			print "Try to reconnect over 5 seconds"
			try:
				xmpp_con.stop(notify=False)
				del xmpp_con
			except:
				traceback.print_exc()
				pass
			time.sleep(5)

if __name__ == "__main__":
	if conf.psyco:
		try:
			import psyco
			psyco.full()
			print "Enabling psyco support."
		except:
			print "Looks like psyco is not installed in your system.",
			print "Psyco acceleration will not be enabled."
			pass
	main()