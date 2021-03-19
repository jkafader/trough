====================
The Design of Trough
====================

Inspiration
===========

On the Archive-It team at Internet Archive, we run many crawlers in parallel, all executing crawl jobs.
Each of the crawlers log many events into their logs per second. At any given time, we expect to be able
to quickly serve analytics on any of the million+ crawls that we've run both historically, and in the last
few minutes.

We tried a number of analytics stores that would work at this scale, but none of them fit the bill. One
came close, a system called Druid that was designed for real-time analytics for advertisements. We found
operations with this system to be too challenging to maintain for a variety of reasons, mostly centered 
around tuning complexity and running java processes at scale in production (for those of you who have
never undertaken this challenge, it involves constant vigilance with reference to memory tuning, lest
the JVM should throw and OOME).

This led to a situation in which we were essentially sure about the features we *didn't want* in our
application -- a list of performance and maintenance challenges. Instead of re-tooling a system which
had many of these challenges baked-in, we decided instead to focus on building a new distributed 
database system, culling the tried-and-true technologies that we had found to be invaluable many times.

Our Query Pattern
-----------------

As we gained experience with previous systems, we found that most of our data was tied to a single sharding
key: the ID number of a given "crawl job" -- a single run of a crawl on a single machine.

Specifically, we thought that we could phrase the problem of analytics at scale as essentially a 
service discovery problem where small shards of analytics data could be segmented on some reasonable
key.

This led to an approach where we considered a number of service discovery systems and distributed configuration
stores -- including some such as `zookeeper`, `etcd` and `zetcd` to name a few. We again knew from broad
experience in our problem that this would be the bottleneck in any distributed design: _how many small 
databases could the system publish discovery data on in parallel?_

We found a match for our needs in a no-SQL database called RethinkDB: it now publishes discovery data for 
and receives TTL heartbeats on ~1.5m small databases. The key attribute of RethinkDB, however, is its
resiliency and ease of operation. The system can sustain writes with 1/3 of its servers missing; reads
with 2/3 of its servers missing. This particular feature of the system is particularly critical when
running servers with no battery backup as we do at Internet Archive.

Goals
=====

Reliability
-----------
From the beginning, trough was designed to be a distributed system. An observation that guided its
development was that a distributed system multiplies, sometimes exponentially, the complexity of any 
given part. We took time to ensure that the algorithms and components involved were as reliable as possible.

Scalability
-----------
We knew at the outset that our system design needed to be able to scale to 10 million or even 100 million+
individual data segments. As such, we chose a service discovery system that had proven itself reliable
in production at this scale (RethinkDB).


Fault Tolerance
---------------
When dealing with very large datasets, it is generally a very good idea to allow the data store to be
as fault-tolerant as possible. This was a great match with SQLite: it allows the atomic data stores to be
very small as compared to the overall size of the dataset, so if any one part fails, it allows the system
to still operate very smoothly around the failed part, and allows the operator to concentrate on correcting
a small atomic data store rather than looking through a gigantic data store for a single bad line or 
corrupted file.


Terminology
===========

**Segment:** a "segment" in trough is a free-standing SQLite database. From the system's overall perspective
these segments are considered an atomic data storage container, or a "segment" of the total data set.

**Write Provisioning:** when a segment is write provisioned in trough, a specific assigned hot storage worker
acquires a "write lock" on the segment. This write lock locks a specific machine as the writer worker, rather
than guaranteeing that a particular process can write to it. While write-provisioned, all queries for the
write-provisioned segment proxy to the machine that holds the write lock, to guarantee consistency at the
expense of resiliency. When write are completed to a writable segment, the segment should be **promoted**

**Promotion:** When a segment is promoted, it is moved to the upstream data store, HDFS, as a canonical
copy of the data. Trough compares segment modification times in HDFS to the modification times on hot
storage workers. If the HDFS copy is *newer* than the copy from the hot tier, it is considered to be
"fresher." At this point, the hot tier worker downloads a copy of the segment from HDFS, overwriting
the local copy. This process of copying down from a more authoritative upstream source gives a convenient
method of overwriting specific segments if they contain inaccurate or out-of-date data. In particular,
this mechanism can be used to overwrite, for example, realtime collected statistical information with
a verified-correct copy of the data later. This allows for a system that quickly collects relatively
accurate realtime statistics and allows them to be overwritten later with a canonical copy of the data.

**Hot Storage Tier:** a segment stored in the hot storage tier is available to be made into a writeable
segment by issuing a "write provisioning" request. Only data stored in the hot storage tier can be
write-provisioned.

**Cold Storage Tier:** Trough allows for data stored in HDFS to also be queried in-place in HDFS over
an NFS connection. Data segments that are stored and queried in this way are slightly slower and
cannot be write-provisioned.

**Consistent Hash Ring:** A consistent hash ring is a data structure that allows a set of target data
to be assigned conveniently and easily to a set of servers. Trough uses N consistent hash rings to model
the problem of requiring N copies of data segments to be published for resiliency. See the wikipedia
entry at https://en.wikipedia.org/wiki/Consistent_hashing#Basic_Technique for more information on 
consistent hash rings more generally. 

Topology
========

The Trough system comprises a few major parts:

RethinkDB
---------
RethinkDB is used by trough as a metadata store that coordinates the synchronization process. For more
information on what data is stored in RethinkDB and how it is used, see the RethinkDB Tables section.


Sync Servers
------------
a set of 3 or more machines that run the same sync server code. They are responsible for 
creating Consistent Hash Rings to assign the set of SQLite segments to the available pool of "workers."
The code that runs on a sync server is covered in ``sync.py`` in the class ``MasterSyncController``

Workers
-------
a set of any number of machines that serve as a storage pool to which the overall set of 
segments can be assigned. For downtime resiliency, Trough assigns multiple copies of "hot" segments
to a pool of workers. The details of which segments are considered hot vs cold can be configured
in the YAML settings file (MINIMUM_ASSIGNMENTS and COLD_STORE_SEGMENTS)

Cold Storage Workers
--------------------
a set of machines that service queries to SQLite segments, reading into
NFS-mounted HDFS. Cold storage workers cannot make their segments writable and may have slower
query times, but do not use local storage.


Interaction with HDFS
=====================

Trough uses HDFS as a storage system to keep canonical copies of data, as well as an NFS-mounted system
for cold storage.

As a Canonical Data Store
-------------------------
Trough expects to disover SQLite data segments (recursively) under a particular HDFS path. It searches
for them based on the value of the HDFS_PATH setting.

As a Cold Storage Data Store
----------------------------
Trough uses an NFS mount to allow cold storage workers to run queries against sqlite segments while
still stored directy in HDFS.

RethinkDB Tables
================

Assignment
----------

When a segment is first detected in HDFS, it is *assigned* to a set of *hot storage workers* by adding
an assignment record to the assignment table.

Services
--------

The services table is trough's service discovery system. After copying a segment down from HDFS, it 
advertises a *service* in the services table to be discovered. As long as the server and segment
remain up-to-date and healthy, the hot storage worker will update the *Time-to-Live* of the service
record stored in this table. This regular update process allows us to automatically fail to another
"up" copy of the data in the case that one or more hot storage workers goes offline.

Lock
----

The lock table records data on which hot storage worker holds a "write lock" (see Terminology) on a given
copy of a segment.




The Shell
=========

Installing and using the shell::

    git clone https://github.com/internetarchive/trough.git
    cd trough
    virtualenv -p python3 venv
    source venv/bin/activate
    pip install -e .
    trough-shell -u rethinkdb://your.server.name/name_of_rethinkdb_database

The last command is likely to be something like::
    trough-shell -u rethinkdb://rdb.your.org/trough_configuration

You can select any of the rethinkdb machines to which the trough configuration database is deployed; their data is carefully kept in sync.

The trough shell help system
----------------------------

After starting the shell, you should be aware that it contains a help system. Trough has a number of unusual commands that it supports which are not part of SQL.

HELP::

    Documented commands (type help <topic>):

    EOF  connect  format  promote  register  show
    bye  exit     infile  quit     select    shred

Trough-specific shell commands
------------------------------

CONNECT::

        Connect to one or more trough "segments" (sqlite databases).
        Usage:

        - CONNECT segment [segment...]
        - CONNECT MATCHING <regex>

        See also SHOW CONNECTIONS

FORMAT::

        Set result output display format. Options:

        - FORMAT TABLE   - tabular format (the default)
        - FORMAT PRETTY  - pretty-printed json
        - FORMAT RAW     - raw json

        With no argument, displays current output format.

PROMOTE::

        Promote connected segments to permanent storage in hdfs.

        Takes no arguments. Only supported in read-write mode.``

REGISTER::

        Register a new schema. Reads the schema from 'schema_file' argument. 

        Usage:

        REGISTER SCHEMA schema_name schema_file
        
        See also: SHOW SCHEMA(S)

SHOW::

        SHOW command, like MySQL. Available subcommands:
        - SHOW TABLES
        - SHOW CREATE TABLE
        - SHOW CONNECTIONS
        - SHOW SCHEMA schema-name
        - SHOW SCHEMAS
        - SHOW SEGMENTS
        - SHOW SEGMENTS MATCHING <regex>

INFILE::

        Read and execute SQL commands from a file.

        Usage:

        INFILE filename

SHRED::


        Delete segments entirely from trough. CAUTION: Not reversible!
        Usage:

        SHRED SEGMENT segment_id [segment_id...]

SQLite SQL dialect
------------------

Trough leverages SQLite's SQL dialect, completely unmodified, to query segments. Rather than writing a 
(new, different, unreliable) SQL variant, we decided to use SQLite's, which is extensively documented.
This also makes writing drivers for ORM systems to interface with trough relatively easy if they already
support SQLite's SQL variant.

Multiple connections
--------------------

Trough's shell allows you to connect to thousands or even millions of data segments simultaneously, and
can send queries efficiently to all connected segments.


Maintenance and FAQs
====================


Known Issues
============

Developer Installation
======================

One of the difficult parts in installing trough is obtaining a copy of libhdfs3. This library can be
exceedingly difficult to locate and install. I've found that the best way to get a copy of it on OS X::

    brew install miniconda
    conda install libhdfs3

This installed libhdfs3 to /usr/local/Caskroom/miniconda/base on my system, but this may vary. You can
find the prefix for your installed miniconda packages via::

    miniconda list

You should symlink or copy the .dylib files from their location here to something like /usr/local/lib
so python can find them easily.

Compiled dpkg distributions are also available here:
https://github.com/jkafader/libhdfs3-deb/

specifically compiled for ubuntu xenial at the moment, but this repo leverages more or less the same
technique of installing miniconda and then using that to install libhdfs3.

