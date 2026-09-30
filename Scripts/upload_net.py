#!/bin/python3

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                           #
#   OpenBench is a chess engine testing framework by Andrew Grant.          #
#   <https://github.com/AndyGrant/OpenBench>  <andrew@grantnet.us>          #
#                                                                           #
#   OpenBench is free software: you can redistribute it and/or modify       #
#   it under the terms of the GNU General Public License as published by    #
#   the Free Software Foundation, either version 3 of the License, or       #
#   (at your option) any later version.                                     #
#                                                                           #
#   OpenBench is distributed in the hope that it will be useful,            #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#   GNU General Public License for more details.                            #
#                                                                           #
#   You should have received a copy of the GNU General Public License       #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.   #
#                                                                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import argparse
import os
import requests

from html.parser import HTMLParser

def url_join(*args):
    # Join a set of URL paths while maintaining the correct format
    return '/'.join([f.lstrip('/').rstrip('/') for f in args]) + '/'

class BannerParser(HTMLParser):


    BANNERS = ('error-message', 'warning-message', 'status-message')

    def __init__(self):
        super().__init__()
        self.banners = {}
        self.current = None
        self.in_pre  = False

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get('class') or '').split()
        if tag == 'div' and (banner := next((x for x in self.BANNERS if x in classes), None)):
            self.current = banner
            self.banners.setdefault(banner, '')
        self.in_pre = self.in_pre or (tag == 'pre' and self.current is not None)

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.in_pre = False
        elif tag == 'div':
            self.current = None

    def handle_data(self, data):
        if self.in_pre:
            self.banners[self.current] += data

def page_banners(html):
    parser = BannerParser()
    parser.feed(html)
    return { name : text.strip() for name, text in parser.banners.items() }

def upload_network():

    # We can use ENV variables for Username, Password, and Server
    req_user   = required=('OPENBENCH_USERNAME' not in os.environ)
    req_pass   = required=('OPENBENCH_PASSWORD' not in os.environ)
    req_server = required=('OPENBENCH_SERVER' not in os.environ)

    # For clarity, seperate out this help text
    help_user   = 'Username. May also be passed as OPENBENCH_USERNAME environment variable'
    help_pass   = 'Password. May also be passed as OPENBENCH_PASSWORD environment variable'
    help_server = '  Server. May also be passed as OPENBENCH_SERVER   environment variable'

    # Parse all arguments, all of which must exist in some form
    p = argparse.ArgumentParser()
    p.add_argument('-U', '--username', help=help_user     , required=req_user  )
    p.add_argument('-P', '--password', help=help_pass     , required=req_pass  )
    p.add_argument('-S', '--server'  , help=help_server   , required=req_server)
    p.add_argument('-E', '--engine'  , help='Engine'      , required=True      )
    p.add_argument('-N', '--name'    , help='Network Name', required=True      )
    p.add_argument('-F', '--file'    , help='File Name'   , required=True      )
    args = p.parse_args()

    # Fallback on ENV variables for Username, Password, and Server
    args.username = args.username if args.username else os.environ['OPENBENCH_USERNAME']
    args.password = args.password if args.password else os.environ['OPENBENCH_PASSWORD']
    args.server   = args.server   if args.server   else os.environ['OPENBENCH_SERVER'  ]

    # All scripts connect through this API point, with an action in the POST data
    url = url_join(args.server, 'scripts')

    # POST payload must contain an action value
    data = {
        'username' : args.username,
        'password' : args.password,
        'engine'   : args.engine,
        'name'     : args.name,
        'action'   : 'UPLOAD_NETWORK',
    }

    # Upload the file and report the status code
    with open(args.file, 'rb') as network:
        r = requests.post(url, data=data, files={ 'netfile' : network })
        print ('Code  : %s' % (r.status_code))

    # Report any error, warning or status messages
    banners = page_banners(r.text)
    for label, banner in (('Error ', 'error-message'), ('Warn  ', 'warning-message'), ('Status', 'status-message')):
        if banner in banners:
            print ('%s: %s' % (label, banners[banner]))

if __name__ == '__main__':
    upload_network()