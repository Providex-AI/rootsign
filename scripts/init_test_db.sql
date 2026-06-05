CREATE DATABASE rootsign_test OWNER rootsign;
\connect rootsign_test
CREATE EXTENSION IF NOT EXISTS timescaledb;
\connect rootsign_dev
CREATE EXTENSION IF NOT EXISTS timescaledb;
