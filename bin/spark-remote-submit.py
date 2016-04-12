import os
import os.path
import requests
import json
import ConfigParser


# # # # # # # # # # # #
#
# Configuration
#
# # # # # # # # # # # #

config = ConfigParser.ConfigParser()
config.read(os.path.join(os.getcwd(), 'project.cfg'))

javaHome = config.get("Hadoop", "javaHome")
hdpVersion = config.get("Hadoop", "hdpVersion")
hadoopNameNode = config.get("Hadoop", "hadoopNameNode")
hadoopResourceManager = config.get("Hadoop", "hadoopResourceManager")
hadoopWebhdfsHost = config.get("Hadoop", "hadoopWebhdfsHost")
remoteSparkJar = config.get("Hadoop", "remoteSparkJar")

projectFolder = config.get("Project", "projectFolder")
appName = config.get("Project", "appName")
appJar = config.get("Project", "appJar")
sparkProperties = config.get("Project", "sparkProperties")
applicationMasterMemory = config.getint("Project", "applicationMasterMemory")
applicationMasterCores = config.getint("Project", "applicationMasterCores")

# computed, not loaded from project.cfg
remoteAppJar = os.path.join(projectFolder, "simple-project.jar")
remoteSparkProperties = os.path.join(projectFolder, sparkProperties)

lzoJar = { 
  "2.3.2.0-2950": "",
  "2.4.0.0-169": "/usr/hdp/2.4.0.0-169/hadoop/lib/hadoop-lzo-0.6.0.2.4.0.0-169.jar"
}

username = ""
password = ""

# # # # # # # # # # # #
#
# Helper functions
#
# # # # # # # # # # # #

def createHdfsPath(path):
  return os.path.join("hdfs://", hadoopNameNode, path.strip("/"))

def webhdfsGetRequest(path, op, allow_redirects=False):
    url = os.path.join(hadoopWebhdfsHost, path.strip("/"))
    response = requests.get("%s?op=%s" % (url, op), allow_redirects=allow_redirects, verify=False, auth=(username, password))
    return response.json()

def webhdfsPutRequest(path, op, allow_redirects=False):
    url = os.path.join(hadoopWebhdfsHost, path.strip("/"))
    response = requests.put("%s?op=%s" % (url, op), "", allow_redirects=allow_redirects, verify=False, auth=(username, password))
    return response

def pathExists(path):
    response = webhdfsGetRequest(path, "GETFILESTATUS")
    return (response.has_key("FileStatus"), response)

def createDir(path):
    response = webhdfsPutRequest(path, "MKDIRS").json()
    return (response.has_key("boolean") and response["boolean"], response)

def uploadFile(localFile, remoteFile):
    response = webhdfsPutRequest(remoteFile, "CREATE&overwrite=true")
    location = response.headers.get("Location")
    if location:
        with open(localFile, "rb") as fd:
            response = requests.put(location, fd, verify=False, auth=(username, password))
            return (True, response.text)
    return(False, "")

def createCacheValue(path, size, timestamp):
    return {
        "resource": createHdfsPath(path),
        "type": "FILE",
        "visibility": "APPLICATION",
        "size": size,
        "timestamp": timestamp
    }

def createNewApplication():
  url = os.path.join(hadoopResourceManager, "cluster/apps/new-application")
  response = requests.post(url, "", verify=False, auth=(username, password))
  return (True, response.json())


def submitSparkJob(sparkJson):
  url = os.path.join(hadoopResourceManager, "cluster/apps")
  response = requests.post(url, sparkJson, headers={"Content-Type": "application/json"}, verify=False, auth=(username, password))
  return response


# # # # # # # # # # # #
#
# Main
#
# # # # # # # # # # # #

print "Getting credentials from environment variable KNOX_CREDENTIALS as username:password..."
if os.environ.get("KNOX_CREDENTIALS"):
  username, password = os.environ["KNOX_CREDENTIALS"].split(":")


print "Checking project folder ..."
if not pathExists(projectFolder):
    ret = createDir(projectFolder)
    if not ret[0]: raise Exception(json.dumps(ret[1]))


print "Uploading App Jar ..."
ret = uploadFile(appJar, remoteAppJar)
if not ret[0]: raise Exception(ret[1])


print "Uploading Spark properties"
with open("spark-yarn.properties.template", "r") as fd:
    properties = fd.read()

with open(sparkProperties, "w") as fd:
    fd.write(properties)

ret = uploadFile(sparkProperties, remoteSparkProperties)
if not ret[0]: raise Exception(ret[1])


print "Creating Spark Job file ..."

ret = pathExists(remoteSparkJar)
if not ret[0]: raise Exception(ret[1])
sparkJarFileStatus = ret[1]["FileStatus"]

ret = pathExists(remoteAppJar)
if not ret[0]: raise Exception(ret[1])
appJarFileStatus = ret[1]["FileStatus"]

ret = pathExists(remoteSparkProperties)
if not ret[0]: raise Exception(ret[1])
sparkPropertiesFileStatus = ret[1]["FileStatus"]

newApp = createNewApplication()

sparkJob = {
  "application-id": newApp[1]["application-id"],
  "application-name": appName,
  "am-container-spec":
  {
    "local-resources":
    {
      "entry":[
        {
          "key": "__spark__.jar",
          "value": createCacheValue(remoteSparkJar, sparkJarFileStatus["length"], sparkJarFileStatus["modificationTime"])
        },
        {
          "key": "__app__.jar",
          "value": createCacheValue(remoteAppJar, appJarFileStatus["length"], appJarFileStatus["modificationTime"])
        },
        {
          "key": "__app__.properties",
          "value": createCacheValue(remoteSparkProperties, sparkPropertiesFileStatus["length"], sparkPropertiesFileStatus["modificationTime"])
        }
      ]
    },
    "commands":
    {
      "command": "{{JAVA_HOME}}/bin/java -server -Xmx1024m " + \
                 "-Dhdp.version=%s " % hdpVersion + \
                 "-Dspark.yarn.app.container.log.dir=/hadoop/yarn/log/rest-api " + \
                 "-Dspark.app.name=%s " % appName + \
                 "org.apache.spark.deploy.yarn.ApplicationMaster " + \
                 # "--properties-file {{PWD}}/__app__.properties " + \
                 "--class IrisApp --jar __app__.jar " + \
                 "--arg '--class' --arg 'IrisApp' " + \
                 "1><LOG_DIR>/AppMaster.stdout " + \
                 "2><LOG_DIR>/AppMaster.stderr"
    },
    "environment":
    {
      "entry":
      [
        {
          "key": "JAVA_HOME",
          "value": javaHome
        },
        {
          "key": "SPARK_YARN_MODE",
          "value": True
        },
        {
          "key": "HDP_VERSION",
          "value": hdpVersion
        },
        {
          "key": "CLASSPATH",
          "value": "{{PWD}}<CPS>__spark__.jar<CPS>" + \
                   "{{PWD}}/__app__.jar" + \
                   "{{PWD}}/__app__.properties<CPS>" + \
                   "{{HADOOP_CONF_DIR}}<CPS>" + \
                   "/usr/hdp/current/hadoop-client/*<CPS>" + \
                   "/usr/hdp/current/hadoop-client/lib/*<CPS>" + \
                   "/usr/hdp/current/hadoop-hdfs-client/*<CPS>" + \
                   "/usr/hdp/current/hadoop-hdfs-client/lib/*<CPS>" + \
                   "/usr/hdp/current/hadoop-yarn-client/*<CPS>" + \
                   "/usr/hdp/current/hadoop-yarn-client/lib/*<CPS>" + \
                   "{{PWD}}/mr-framework/hadoop/share/hadoop/common/*<CPS>" + \
                   "{{PWD}}/mr-framework/hadoop/share/hadoop/common/lib/*<CPS>" + \
                   "{{PWD}}/mr-framework/hadoop/share/hadoop/yarn/*<CPS>" + \
                   "{{PWD}}/mr-framework/hadoop/share/hadoop/yarn/lib/*<CPS>" + \
                   "{{PWD}}/mr-framework/hadoop/share/hadoop/hdfs/*<CPS>" + \
                   "{{PWD}}/mr-framework/hadoop/share/hadoop/hdfs/lib/*<CPS>" + \
                   "{{PWD}}/mr-framework/hadoop/share/hadoop/tools/lib/*<CPS>" + \
                   "%s<CPS>" % lzoJar[hdpVersion] + \
                   "/etc/hadoop/conf/secure<CPS>"
        },
        {"key":
          "SPARK_YARN_CACHE_FILES",
          "value": "%s#__app__.jar,%s#__spark__.jar" % (createHdfsPath(remoteAppJar), createHdfsPath(remoteSparkJar))
        },
        {"key":
          "SPARK_YARN_CACHE_FILES_FILE_SIZES",
          "value": "%d,%d" % (appJarFileStatus["length"], sparkJarFileStatus["length"])
        },
        {"key":
          "SPARK_YARN_CACHE_FILES_TIME_STAMPS",
          "value": "%d,%d" % (appJarFileStatus["modificationTime"], sparkJarFileStatus["modificationTime"])
        },
        {"key":
          "SPARK_YARN_CACHE_FILES_VISIBILITIES",
          "value": "PUBLIC,PRIVATE"
        },
      ]
    }
  },
  "unmanaged-AM": False,
  "max-app-attempts": 2,
  "resource": {  
    "memory": applicationMasterMemory,
    "vCores": applicationMasterCores
  },
  "application-type": "YARN",
  "keep-containers-across-application-attempts": False
}

print "Submitting Spark Job ..."

sparkJobJson = json.dumps(sparkJob, indent=2, sort_keys=True)
with open("spark-yarn.json", "w") as fd:
  fd.write(sparkJobJson)

response = submitSparkJob(sparkJobJson)
print "\n==> Job tracking URL:", response.headers["Location"].replace("apps//", "apps/")

