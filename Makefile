spec: pubsubhubbub-core-0.4.html

pubsubhubbub-core-0.4.html: pubsubhubbub-core-0.4.xml
	xml2rfc pubsubhubbub-core-0.4.xml pubsubhubbub-core-0.4.html
	cat pubsubhubbub-core-0.4.html | head -n -1 > out.html
	cat analytics.txt >> out.html
	mv out.html pubsubhubbub-core-0.4.html

