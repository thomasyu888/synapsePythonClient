import ConfigParser
import collections
import os
import json
import base64
import urllib
import urlparse
import zipfile
import requests
import os.path
import mimetypes
import stat
import pkg_resources
import webbrowser
import sys
import time

import synapseclient.utils as utils
from synapseclient.version_check import version_check
from synapseclient.utils import id_of, get_properties, KB, MB
from synapseclient.annotations import from_synapse_annotations, to_synapse_annotations
from synapseclient.activity import Activity
from synapseclient.entity import Entity, File, split_entity_namespaces, is_versionable, is_locationable
from synapseclient.evaluation import Evaluation, Submission, SubmissionStatus
from synapseclient.retry import RetryRequest
from synapseclient.wiki import Wiki


PRODUCTION_ENDPOINTS = {'repoEndpoint':'https://repo-prod.prod.sagebase.org/repo/v1',
                        'authEndpoint':'https://auth-prod.prod.sagebase.org/auth/v1',
                        'fileHandleEndpoint':'https://file-prod.prod.sagebase.org/file/v1', 
                        'portalEndpoint':'https://synapse.org/'}

STAGING_ENDPOINTS    = {'repoEndpoint':'https://repo-staging.prod.sagebase.org/repo/v1',
                        'authEndpoint':'https://auth-staging.prod.sagebase.org/auth/v1',
                        'fileHandleEndpoint':'https://file-staging.prod.sagebase.org/file/v1', 
                        'portalEndpoint':'https://staging.synapse.org/'}

__version__=json.loads(pkg_resources.resource_string('synapseclient', 'synapsePythonClient'))['latestVersion']
CACHE_DIR=os.path.join(os.path.expanduser('~'), '.synapseCache', 'python')  #TODO change to /data when storing files as md5
CONFIG_FILE=os.path.join(os.path.expanduser('~'), '.synapseConfig')
FILE_BUFFER_SIZE = 4*KB
CHUNK_SIZE = 5*MB
CHUNK_UPLOAD_POLL_INTERVAL = 1 # second
ROOT_ENTITY = 'syn4489'


## defines the standard retry policy applied to the rest methods
STANDARD_RETRY_REQUEST = RetryRequest(retry_status_codes=[502,503],
                                      retry_errors=[],
                                      retry_exceptions=['Timeout', 'timeout'],
                                      retries=3, wait=1, back_off=2, verbose=False)

## add additional mimetypes
mimetypes.add_type('text/x-r', '.R', strict=False)
mimetypes.add_type('text/x-r', '.r', strict=False)


class Synapse:
    """
    Constructs a Python client object for the Synapse repository service
    
    :param repoEndpoint:          Location of Synapse repository
    :param authEndpoint:          Location of authentication service
    :param fileHandleEndpoint:    Location of file service
    :param portalEndpoint:        Location of the website
    :param serviceTimeoutSeconds: Wait time before timeout (currently unused) 
    :param debug:                 Print debugging messages if True
    :param skip_checks:           Skip version and endpoint checks
    
    Typically, no parameters are needed::
    
        import synapseclient
        syn = synapseclient.Synapse()
        
    See: 
    
    - :py:func:`synapseclient.Synapse.login`
    - :py:func:`synapseclient.Synapse.setEndpoints`
    """

    def __init__(self, repoEndpoint=None, authEndpoint=None, fileHandleEndpoint=None, portalEndpoint=None, 
                 serviceTimeoutSeconds=30, debug=False, skip_checks=False):
        self.cacheDir = os.path.expanduser(CACHE_DIR)
        # Create the cache directory if it does not exist
        try:
            os.makedirs(self.cacheDir)
        except OSError as exception:
            if exception.errno != os.errno.EEXIST:
                raise

        # Create the ~/.synapseconfig file if it does not exist
        if not os.path.isfile(CONFIG_FILE):
            config = ConfigParser.ConfigParser()
            config.add_section('authentication')
            with open(CONFIG_FILE, 'w') as configfile:
                config.write(configfile)
                
        self.setEndpoints(repoEndpoint, authEndpoint, fileHandleEndpoint, portalEndpoint, skip_checks)
        self.headers = {'content-type': 'application/json', 'Accept': 'application/json', 'request_profile':'False'}

        ## TODO serviceTimeoutSeconds is never used. Either use it or delete it.
        self.serviceTimeoutSeconds = serviceTimeoutSeconds 
        self.debug = debug
        self.sessionToken = None
        self.skip_checks = skip_checks
        
    
    def setEndpoints(self, repoEndpoint=None, authEndpoint=None, fileHandleEndpoint=None, portalEndpoint=None, skip_checks=False):
        """
        Sets the locations for each of the Synapse services (mostly useful for testing).

        :param repoEndpoint:          Location of synapse repository
        :param authEndpoint:          Location of authentication service
        :param fileHandleEndpoint:    Location of file service
        :param portalEndpoint:        Location of the website
        :param skip_checks:           Skip version and endpoint checks
        
        To switch between staging and production endpoints::
            
            syn.setEndpoints(**synapseclient.client.STAGING_ENDPOINTS)
            syn.setEndpoints(**synapseclient.client.PRODUCTION_ENDPOINTS)
            
        """
        
        # If endpoints aren't specified, look in the config file
        try:
            config = ConfigParser.ConfigParser()
            config.read(CONFIG_FILE)
            if config.has_section('endpoints'):
                if repoEndpoint is None and config.has_option('endpoints', 'repoEndpoint'):
                    repoEndpoint = config.get('endpoints', 'repoEndpoint')
                         
                if authEndpoint is None and config.has_option('endpoints', 'authEndpoint'):
                    authEndpoint = config.get('endpoints', 'authEndpoint')
                         
                if fileHandleEndpoint is None and config.has_option('endpoints', 'fileHandleEndpoint'):
                    fileHandleEndpoint = config.get('endpoints', 'fileHandleEndpoint')
                   
                if portalEndpoint is None and config.has_option('endpoints', 'portalEndpoint'):
                    portalEndpoint = config.get('endpoints', 'portalEndpoint')
        except ConfigParser.Error:
            sys.stderr.write('Error parsing synapse config file: %s' % CONFIG_FILE)
            raise

        ## endpoints default to production
        if repoEndpoint is None:
            repoEndpoint = PRODUCTION_ENDPOINTS['repoEndpoint']
        if authEndpoint is None:
            authEndpoint = PRODUCTION_ENDPOINTS['authEndpoint']
        if fileHandleEndpoint is None:
            fileHandleEndpoint = PRODUCTION_ENDPOINTS['fileHandleEndpoint']
        if portalEndpoint is None:
            portalEndpoint = PRODUCTION_ENDPOINTS['portalEndpoint']

        ## update endpoints if we get redirected
        if not skip_checks:
            resp=requests.get(repoEndpoint, allow_redirects=False)
            if resp.status_code==301:
                repoEndpoint=resp.headers['location']
                
            resp=requests.get(authEndpoint, allow_redirects=False)
            if resp.status_code==301:
                authEndpoint=resp.headers['location']
                
            resp=requests.get(fileHandleEndpoint, allow_redirects=False)
            if resp.status_code==301:
                fileHandleEndpoint=resp.headers['location']
                
            resp=requests.get(portalEndpoint, allow_redirects=False)
            if resp.status_code==301:
                portalEndpoint=resp.headers['location']

        self.repoEndpoint = repoEndpoint
        self.authEndpoint = authEndpoint
        self.fileHandleEndpoint = fileHandleEndpoint
        self.portalEndpoint = portalEndpoint


    def _storeTimingProfile(self, resp):
        # Stores timing information for the last call if request_profile was set
        
        if self.headers.get('request_profile', 'False')=='True':
            profile_data = None
            for k,v in resp.getheaders():
                if k == "profile_response_object":
                    profile_data = v
                    break
            self.profile_data = json.loads(base64.b64decode(profile_data))


    def login(self, email=None, password=None, sessionToken=None):
        """
        Authenticates and retrieves a session token by using (in order of preference):
        
        1) supplied email and password
        2) supplied session token
        3) check for a saved session token in the configuration file
        4) check for a saved email and password in the configuraton file
        """

        # check version before logging in
        if not self.skip_checks: version_check()
        
        # Open up the config file
        config = ConfigParser.ConfigParser()
        config.read(CONFIG_FILE)

        if (email==None or password==None):
            if sessionToken is not None or config.has_option('authentication', 'sessiontoken'):
                token = {}
                
                # Try to grab an existing session token
                if sessionToken is None:
                    token["sessionToken"] = config.get('authentication', 'sessiontoken')
                else: 
                    token["sessionToken"] = sessionToken
                    
                # Validate the session token
                try:
                    response = self.restPUT('/session', body=json.dumps(token), endpoint=self.authEndpoint)
                    
                    # Success!
                    self.headers["sessionToken"] = token["sessionToken"]
                    self.sessionToken = token["sessionToken"]
                    
                    # Save the session token if the user supplied it
                    if sessionToken is not None:
                        if not config.has_section('authentication'):
                            config.add_section('authentication')
                        config.set('authentication', 'sessionToken', token["sessionToken"])
                        with open(CONFIG_FILE, 'w') as configfile:
                            config.write(configfile)
                    return
                except requests.exceptions.HTTPError as err:
                    # Bad session token
                    if err.response.status_code == 404 and sessionToken is not None:
                        raise Exception("LOGIN FAILED: supplied session token (%s) is invalid" % token["sessionToken"])
                    
                    # Re-raise the exception if the error is something else
                    elif sessionToken is not None:
                        raise err
                        
                    else:
                        sys.stderr.write('Note: stored session token is invalid\n')
                # Assume the stored session token is expired and try the other parts of the config 
            
            if (config.has_option('authentication', 'username') and config.has_option('authentication', 'password')):
                email = config.get('authentication', 'username')
                password = config.get('authentication', 'password')
            else:
                raise Exception("LOGIN FAILED: no credentials provided")

        # Disable profiling during login and proceed with authentication
        self.headers['request_profile'], orig_request_profile='False', self.headers['request_profile']

        try:
            req = {"email":email, "password":password}
            session = self.restPOST('/session', body=json.dumps(req), endpoint=self.authEndpoint)
        except requests.exceptions.HTTPError as err:
            if err.response.status_code == 400:
                raise Exception("LOGIN FAILED: invalid username or password")
            else:
                raise err

        self.sessionToken = session["sessionToken"]
        self.headers["sessionToken"] = session["sessionToken"]
                    
        # Save the session token
        if not config.has_section('authentication'):
            config.add_section('authentication')
        config.set('authentication', 'sessionToken', session["sessionToken"])
        with open(CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
        return
        
        self.headers['request_profile'] = orig_request_profile


    def _loggedIn(self):
        # Test whether the user is logged in to Synapse.
        if self.sessionToken is None:
            return False
        url = '%s/userProfile' % (self.repoEndpoint,)
        response = requests.get(url, headers=self.headers)
        if response.status_code==401:
            ## bad or expired session token
            return False
        response.raise_for_status()
        user = response.json()
        if 'displayName' in user:
            if user['displayName']=='Anonymous':
                ## no session token, not logged in
                return False
            return user['displayName']
        return False
        
        
    def logout(self, local=False):
        """
        Invalidates authentication
        
        :param local: Set as True to logout locally, otherwise all sessions are logged out
        """

        # Logout globally
        # Note: If self.headers['sessionToken'] is deleted or changed, 
        #       global logout will not actually logout (i.e. session token will still be valid)
        if not local: self.restDELETE('/session', endpoint=self.authEndpoint)
            
        # Remove the in-memory session tokens
        del self.sessionToken
        del self.headers["sessionToken"]
        
        # Remove the session token from the config file
        config = ConfigParser.ConfigParser()
        config.read(CONFIG_FILE)
        if config.has_option('authentication', 'sessionToken'):
            config.remove_option('authentication', 'sessionToken')
        with open(CONFIG_FILE, 'w') as configfile:
            config.write(configfile)
            


    def getUserProfile(self, id=None):
        """
        Get the details about a Synapse user.  
        Retrieves information on the current user if 'id' is omitted.
        
        :param id: The 'ownerId' of a user
        
        :returns: JSON-object
        """
        uri = '/userProfile/%s' % ('' if id is None else str(id),)
        return self.restGET(uri)


    def onweb(self, entity, subpageId=None):
        """
        Opens up a browser window to the entity page or wiki-subpage.
        
        :param entity:    Either an Entity or a Synapse ID
        :param subpageId: (Optional) ID of one of the wiki's sub-pages
        """
        if subpageId is None:
            webbrowser.open("%s#!Synapse:%s" % (self.portalEndpoint, id_of(entity)))
        else: 
            webbrowser.open("%s#!Wiki:%s/ENTITY/%s" % (self.portalEndpoint, id_of(entity), subpageId))


    def printEntity(self, entity):
        """Pretty prints an Entity."""
        if utils.is_synapse_id(entity):
            entity = self._getEntity(entity)
        try:
            print json.dumps(entity, sort_keys=True, indent=2)
        except TypeError:
            print str(entity)



    ############################################################
    # get / store methods
    ############################################################

    def get(self, entity, **kwargs):
        """
        Gets a Synapse entity from the repository service.
        
        :param entity:       A Synapse ID, a Synapse Entity object, 
                             or a plain dictionary in which 'id' maps to a Synapse ID
        :param version:      The specific version to get.
                             Defaults to the most recent version.
        :param downloadFile: Whether associated files(s) should be downloaded.  
                             Defaults to True

        :returns: A new Synapse Entity object of the appropriate type
        """

        ##synapse.get(id, version, downloadFile=True, downloadLocation=None, ifcollision="keep.both", load=False)
        ## optional parameters
        version = kwargs.get('version', None)   #This ignores the version of entity if it is mappable
        downloadFile = kwargs.get('downloadFile', True)

        ## If an entity is given without an ID, try to find it by parentId and name.
        ## If the user forgets to catch the return value of a syn.store(e), this allows
        ## them to recover by doing: e = syn.get(e).
        if isinstance(entity, collections.Mapping) and (not entity.get('id',None)) and entity.get('name',None):
            entity = self._findEntityIdByNameAndParent(entity['name'], entity.get('parentId',ROOT_ENTITY))

        ## EntityBundle bit-flags
        ## see: the Java class org.sagebionetworks.repo.model.EntityBundle
        ## ENTITY                    = 0x1
        ## ANNOTATIONS               = 0x2
        ## PERMISSIONS               = 0x4
        ## ENTITY_PATH               = 0x8
        ## ENTITY_REFERENCEDBY       = 0x10
        ## HAS_CHILDREN              = 0x20
        ## ACL                       = 0x40
        ## ACCESS_REQUIREMENTS       = 0x200
        ## UNMET_ACCESS_REQUIREMENTS = 0x400
        ## FILE_HANDLES              = 0x800

        #Get the entity bundle
        if version:
            uri = '/entity/%s/version/%d/bundle?mask=%d' %(id_of(entity), version, (0x800 + 0x400 + 2 + 1))
        else:
            uri = '/entity/%s/bundle?mask=%d' %(id_of(entity), (0x800 + 0x400 + 2 + 1))
        bundle=self.restGET(uri)

        ## check for unmet access requirements
        if len(bundle['unmetAccessRequirements']) > 0:
            sys.stderr.write("\nWARNING: This entity has access restrictions. Please visit the web page for this entity (syn.onweb(\"%s\")). Click the downward pointing arrow next to the file's name to review and fulfill its download requirement(s).\n" % id_of(entity))

        local_state = entity.local_state() if isinstance(entity, Entity) else None
        properties = bundle['entity']
        annotations = from_synapse_annotations(bundle['annotations'])

        ## return a fresh copy of the entity
        entity = Entity.create(properties, annotations, local_state)

        ## for external URLs, we want to retrieve the URL from the fileHandle
        ## Note: fileHandles will be empty if there are unmet access requirements
        if isinstance(entity, File):
            for fh in bundle['fileHandles']:
                if fh['id']==bundle['entity']['dataFileHandleId']:
                    entity.md5=fh.get('contentMd5', '')
                    entity.fileSize=fh.get('contentSize', None)
                    if fh['concreteType'] == 'org.sagebionetworks.repo.model.file.ExternalFileHandle':
                        entity['externalURL'] = fh['externalURL']
                        entity['synapseStore'] = False

                        ## if it's a file URL, fill in the path whether downloadFile is True or not,
                        if fh['externalURL'].startswith('file:'):
                            entity.update(utils.file_url_to_path(fh['externalURL'], verify_exists=True))

                        ## by default, we download external URLs
                        elif downloadFile:
                            entity.update(self._downloadFileEntity(entity))

                    ## by default, we download files stored in Synapse
                    elif downloadFile:
                        entity.update(self._downloadFileEntity(entity))

        #TODO version, here
        if downloadFile and is_locationable(entity):
            entity = self._downloadLocations(entity)

        return entity


    #TODO implement createOrUpdate flag - new entity w/ same name as an existing entity turns into an update
    def store(self, obj, **kwargs):
        """
        Creates a new Entity or updates an existing Entity, 
        uploading any files in the process.

        :params obj:      A Synapse Entity, Evaluation, or Wiki
        :params used:     The Entity, Synapse ID, or URL 
                          used to create the object
        :params executed: The Entity, Synapse ID, or URL 
                          representing code executed to create the object
        :params activity: Activity object specifying the user's provenance

        :returns: A Synapse Entity, Evaluation, or Wiki
        """

        # store(entity, used, executed, activityName=None, 
        #               activityDescription=None, createOrUpdate=T, forceVersion=T, isRestricted=F)
        # store(entity, activity, createOrUpdate=T, forceVersion=T, isRestricted=F)
        createOrUpdate = kwargs.get('createOrUpdate', True)

        #Handle all non entity objects
        if not (isinstance(obj, Entity) or type(obj)==dict):
            if 'id' in obj: #If Id present update 
                obj.update(self.restPUT(obj.putURI(), obj.json()))
                return obj
            try: #if no id attempt to post the object
                obj.update(self.restPOST(obj.postURI(), obj.json())) 
                return obj
            except requests.exceptions.HTTPError as err:
                # If already present and we want to update attempt to get the object content
                # If already present and we want to update attempt to get the object content
                # TODO remove status code 500 after PLFM-1905 goes in to production
                if createOrUpdate and (err.response.status_code==500 or err.response.status_code==409):
                    newObj=self.restGET(obj.getByNameURI(obj.name))
                    newObj.update(obj)
                    obj=obj.__class__(**newObj)
                    obj.update(self.restPUT(obj.putURI(), obj.json()))
                    return obj
                raise

        #If input object is an Entity...
        entity = obj
        properties,annotations,local_state = split_entity_namespaces(entity)

        #TODO: can this be factored out?
        ## need to upload a FileEntity?
        ##   create FileHandle first, then create or update entity
        if entity['entityType'] == File._synapse_entity_type and entity.get('path',False):
            if 'dataFileHandleId' not in properties or True: #TODO: file_cache.local_file_has_changed(entity.path):
                synapseStore = entity.get('synapseStore', None)
                fileHandle = self._uploadToFileHandleService(entity.path, synapseStore=synapseStore)
                properties['dataFileHandleId'] = fileHandle['id']

        ## need to upload a Locationable?
        ##   create the entity first, then upload file, then update entity, later
        if is_locationable(entity):
            ## for Locationables, entity must exist before upload
            if not 'id' in entity:
                properties = self._createEntity(properties)
            ## TODO is this a bug??
            ## TODO is this necessary?
            if 'path' in entity:
                path = annotations.pop('path')
                properties.update(self._uploadFileAsLocation(properties, path))
        #--end--TODO: can this be factored out?

        #TODO deal with access restrictions

        ## create or update entity in synapse
        if 'id' in properties:
            properties = self._updateEntity(properties)
        else:
            try:
                properties = self._createEntity(properties)
            except requests.exceptions.HTTPError as ex:
                ## if "409 Client Error: Conflict", update the existing entity
                ## with same name and parent
                if createOrUpdate and hasattr(ex, 'response') and ex.response.status_code==409:
                    existing_entity_id = self._findEntityIdByNameAndParent(properties['name'], properties.get('parentId', None))
                    if existing_entity_id:
                        existing_entity = self._getEntity(existing_entity_id)
                        ## Need some fields from the existing entity: id, etag
                        ## and version info.
                        existing_entity.update(properties)
                        properties = self._updateEntity(existing_entity)
                    else:
                        raise
                else:
                    raise

        annotations['etag'] = properties['etag']

        ## update annotations
        annotations = self.setAnnotations(properties, annotations)
        properties['etag'] = annotations['etag']


        ## if used or executed given, create an Activity object
        activity = kwargs.get('activity', None)
        used = kwargs.get('used', None)
        executed = kwargs.get('executed', None)
        if used or executed:
            if activity is not None:
                raise Exception('Provenance can be specified as an Activity object or as used/executed item(s), but not both.')
            activityName = kwargs.get('activityName', None)
            activityDescription = kwargs.get('activityDescription', None)
            activity = Activity(name=activityName, description=activityDescription, used=used, executed=executed)

        ## if we have an activity, set it as the entity's provenance record
        if activity:
            activity = self.setProvenance(properties, activity)
            ## etag has changed, so get new entity
            properties = self._getEntity(properties)

        ## return a new Entity object
        return Entity.create(properties, annotations, local_state)


    def delete(self, obj):
        """
        Removes an object from Synapse.
        
        :param obj: An existing object stored on Synapse 
                    such as Evaluation, File, Project, WikiPage etc
        """
        if isinstance(obj, basestring): #Handle all strings as entity id for backward compatibility
            self.restDELETE(uri='/entity/'+id_of(obj))
        else:
            self.restDELETE(obj.deleteURI())


    ############################################################
    # older high level methods
    ############################################################

    def getEntity(self, entity, version=None):
        """
        Retrieves metadata about an Entity from the Synapse Repository.
        
        :param entity: A Synapse ID or dictionary describing an Entity
        
        :returns: An Entity object
        """

        return self.get(entity, version=version, downloadFile=False)


    def loadEntity(self, entity):
        """
        Downloads and attempts to load the contents of an entity into memory.
        TODO_Sphinx: Currently only performs download.
        
        :param entity: Either a string or dictionary representing an Entity
        
        :returns: An Entity object
        """

        #TODO: Try to load the entity into memory as well.
        #This will be depenendent on the type of entity.
        print 'WARNING!: THIS ONLY DOWNLOADS ENTITIES!'
        return self.downloadEntity(entity)


    #TODO factor out common code with store? delegate to store? delegate store to createEntity?
    #TODO should this method upload files?
    def createEntity(self, entity, used=None, executed=None, **kwargs):
        """
        Create a new entity in the Synapse Repository according to entity JSON object.
     
        :param entity:   An Entity object or dictionary
        :param used:     An Entity, Synapse ID, URL, or object/list 
                         representing data used to create this Entity
        :param executed: An Entity, Synapse ID, URL, or object/list 
                         representing code executed to create this Entity
        
        :returns: An updated Entity object
        """

        ## make sure we're creating a new entity
        if 'id' in entity:
            raise Exception('Called createEntity on an entity with an ID (%s)' % str(id_of(entity)))

        # TODO: delegate to store?
        # return self.store(entity, used=used, executed=executed)
        properties,annotations,local_state = split_entity_namespaces(entity)

        properties = self._createEntity(properties)
        annotations['etag'] = properties['etag']

        ## set annotations
        annotations = self.setAnnotations(properties, annotations)
        properties['etag'] = annotations['etag']

        ## if used or executed given, create an Activity object
        activity = kwargs['activity'] if 'activity' in kwargs else None
        if used or executed:
            if activity is not None:
                raise Exception('Provenance can be specified as an Activity object or as used/executed item(s), but not both.')
            activityName = kwargs['activityName'] if 'activityName' in kwargs else None
            activityDescription = kwargs['activityDescription'] if 'activityDescription' in kwargs else None
            activity = Activity(name=activityName, description=activityDescription, used=used, executed=executed)

        ## if we have an activity, set it as the entity's provenance record
        if activity:
            activity = self.setProvenance(properties, activity)
            ## etag has changed, so get new entity
            properties = self._getEntity(properties)

        ## return a new Entity object
        return Entity.create(properties, annotations, local_state)


    #TODO remove?
    #TODO delegate to store? createEntity?
    def _createFileEntity(self, entity, filename=None, used=None, executed=None):
        #Determine if we want to upload or store the url
        #TODO this should be determined by a parameter not based on magic
        #TODO _createFileEntity and uploadFile are kinda redundant - pick one or fold into createEntity
        
        if filename is None:
            if 'path' in entity:
                filename = entity.path
            else:
                raise Exception('can\'t create a File entity without a file path or URL')
        if utils.is_url(filename):
            fileHandle = self._addURLtoFileHandleService(filename)
            entity['dataFileHandleId'] = fileHandle['id']
        else:
            fileHandle = self._chunkedUploadFile(filename)
            entity['dataFileHandleId'] = fileHandle['id']
        if 'entityType' not in entity:
            entity['entityType'] = 'org.sagebionetworks.repo.model.FileEntity'
        return self.createEntity(entity, used=used, executed=executed)


    #TODO factor out common code with store? delegate to store? delegate store to updateEntity?
    #TODO should this method upload files?
    def updateEntity(self, entity, used=None, executed=None, incrementVersion=False, versionLabel=None, **kwargs):
        """
        **Deprecated**
        
        Use :py:func:`synapseclient.Synapse.store` or :py:func:`synapseclient.Synapse.update`
        """

        if not entity:
            raise Exception("entity cannot be empty")
        if 'id' not in entity:
            raise Exception("A entity without an 'id' can't be updated")

        properties,annotations,local_state = split_entity_namespaces(entity)

        properties = self._updateEntity(properties, incrementVersion, versionLabel)
        annotations['etag'] = properties['etag']

        annotations = self.setAnnotations(properties, annotations)
        properties['etag'] = annotations['etag']

        ## if used or executed given, create an Activity object
        activity = kwargs['activity'] if 'activity' in kwargs else None
        if used or executed:
            if activity is not None:
                raise Exception('Provenance can be specified as an Activity object or as used/executed item(s), but not both.')
            activityName = kwargs['activityName'] if 'activityName' in kwargs else None
            activityDescription = kwargs['activityDescription'] if 'activityDescription' in kwargs else None
            activity = Activity(name=activityName, description=activityDescription, used=used, executed=executed)

        ## if we have an activity, set it as the entity's provenance record
        if activity:
            activity = self.setProvenance(properties, activity)
            ## etag has changed, so get new entity
            properties = self._getEntity(properties)

        ## return a new Entity object
        return Entity.create(properties, annotations, local_state)


    def deleteEntity(self, entity):
        """
        **Deprecated**
        
        Use :py:func:`synapseclient.Synapse.delete`
        """
        self.delete(entity)


    #TODO: delegate to store?
    def uploadFile(self, entity, filename=None, used=None, executed=None):
        """
        **Deprecated**
        
        Use :py:func:`synapseclient.Synapse.store`
        """
        ## if we got a synapse ID as a string, get the entity from synapse
        if isinstance(entity, basestring):
            if not filename:
                raise Exception('No filename specified in call to uploadFile')
            entity = self._getEntity(entity)

        if filename is None:
            if 'path' in entity:
                filename = entity['path']
            else:
                raise Exception('can\'t upload a file without a file path or URL')

        ## default name of entity to be the name of the file
        if 'name' not in entity or entity['name'] is None:
            entity['name'] = os.path.basename(filename)

        ## if we have an old location-able object use the deprecated file upload method
        if is_locationable(entity):
            if not 'id' in entity:
                entity = self._createEntity(entity)
            entity.update(self._uploadFileAsLocation(entity, filename))

        else:
            ## if we haven't specified the entity type, make it a FileEntity
            if 'entityType' not in entity:
                entity['entityType'] = 'org.sagebionetworks.repo.model.FileEntity'

            if entity['entityType'] != 'org.sagebionetworks.repo.model.FileEntity':
                raise Exception('Files can only be uploaded to FileEntity entities')

            synapseStore = entity['synapseStore'] if 'synapseStore' in entity else None
            fileHandle = self._uploadToFileHandleService(filename, synapseStore=synapseStore)

            ## for some reason, posting
            entity['dataFileHandleId'] = fileHandle['id']

        if 'id' in entity:
            return self.updateEntity(entity, used=used, executed=executed)
        else:
            return self.createEntity(entity, used=used, executed=executed)


    def downloadEntity(self, entity, version=None):
        """
        **Deprecated**
        
        Use :py:func:`synapseclient.Synapse.get`
        """
        return self.get(entity, version=version, downloadFile=True)



    ############################################################
    # get/set Annotations
    ############################################################

    def getAnnotations(self, entity, version=None):
        """
        Retrieve annotations for an Entity in the Synapse Repository.
        
        :param entity:  An Entity or Synapse ID to lookup
        :param version: The version of the Entity to retrieve
        
        :returns: A dictionary
        """
        ## only use versioned URLs on request.
        ## note that using the versioned URL results in a zero-ed out etag,
        ## even if the version is the most recent. See: PLFM-1874
        if version:
            uri = '/entity/%s/version/%s/annotations' % (id_of(entity), str(version),)
        else:
            uri = '/entity/%s/annotations' % id_of(entity)
        return from_synapse_annotations(self.restGET(uri))        


    def setAnnotations(self, entity, annotations={}, **kwargs):
        """
        Store annotations for an Entity in the Synapse Repository.

        :param entity:      An Entity or Synapse ID to updatae
        :param annotations: A dictionary in Synapse format or a default Python format
        :param kwargs:      TODO_Sphinx
        
        :returns: A dictionary
        """
        uri = '/entity/%s/annotations' % id_of(entity)

        ## update annotations with keyword args
        annotations.update(kwargs)

        synapseAnnos = to_synapse_annotations(annotations)
        synapseAnnos['id'] = id_of(entity)
        if 'etag' in entity and 'etag' not in synapseAnnos:
            synapseAnnos['etag'] = entity['etag']

        return from_synapse_annotations(self.restPUT(uri, json.dumps(synapseAnnos) ))



    ############################################################
    # Query
    ############################################################

    def query(self, queryStr):
        """
        Query for Synapse entities.  
        See `in-depth documentation <https://sagebionetworks.jira.com/wiki/display/PLFM/Repository+Service+API#RepositoryServiceAPI-QueryAPI>`_.

        Example::
        
            syn.query("select id, name from entity where entity.parentId=='syn449742'")
        """
        if(self.debug): print 'About to query %s' % (queryStr)
        return self.restGET('/query?query=' + urllib.quote(queryStr))
        # response = self.restGET('/query?query=' + urllib.quote(queryStr))
        # 
        # for res in response['results']:
        #     yield res


    ############################################################
    # ACL manipulation
    ############################################################

    def _getBenefactor(self, entity):
        # An Entity gets its ACL from its benefactor
        return self.restGET('/entity/%s/benefactor' % id_of(entity))

    def _getACL(self, entity):
        benefactor = self._getBenefactor(entity)

        ## get the ACL from the benefactor (which may be the entity itself)
        uri = '/entity/%s/acl' % (benefactor['id'],)        
        return self.restGET(uri)


    def _storeACL(self, entity, acl):
        entity_id = id_of(entity)

        ## get benefactor. (An entity gets its ACL from its benefactor.)
        uri = '/entity/%s/benefactor' % entity_id
        benefactor = self.restGET(uri)

        ## update or create new ACL
        uri = '/entity/%s/acl' % entity_id
        if benefactor['id']==entity_id:
            return self.restPUT(uri, json.dumps(acl))
        else:
            return self.restPOST(uri,json.dumps(acl))


    def getPermissions(self, entity, principalId):
        """
        Get the permissions that a user or group has on an Entity.

        :param entity:      An Entity or Synapse ID to lookup
        :param principalId: TODO_Sphinx
        
        :returns: An array containing some combination of 
                  ['READ', 'CREATE', 'UPDATE', 'DELETE', 'CHANGE_PERMISSIONS']
                  or an empty array
        """

        #TODO look up user by email?
        #TODO what if user has permissions by membership in a group?
        acl = self._getACL(entity)
        for permissions in acl['resourceAccess']:
            if 'principalId' in permissions and permissions['principalId'] == int(principalId):
                return permissions['accessType']
        return []


    def setPermissions(self, entity, principalId, accessType=['READ'], modify_benefactor=False, warn_if_inherits=True):
        """
        Sets permission that a user or group has on an Entity.
        An Entity may have its own ACL or inherit its ACL from a benefactor.  

        :param entity:            An Entity or Synapse ID to modify
        :param principalId:       TODO_Sphinx
        :param accessType:        TODO_Sphinx
        :param modify_benefactor: Set as True when modifying a benefactor's ACL
        :param warn_if_inherits:  Set as False, when creating a new ACL. 
                                  Trying to modify the ACL of an Entity that 
                                  inherits its ACL will result in a warning
        
        :returns: TODO_Sphinx
        """

        benefactor = self._getBenefactor(entity)

        if benefactor['id'] != id_of(entity):
            if modify_benefactor:
                entity = benefactor
            elif warn_if_inherits:
                sys.stderr.write(utils.normalize_whitespace(
                    '''Warning: Creating an ACL for entity %s, which formerly inherited
                       access control from a benefactor entity, "%s" (%s).''' % 
                       (id_of(entity), benefactor['name'], benefactor['id'],))+'\n')

        principalId = int(principalId)

        acl = self._getACL(entity)

        ## find existing permissions
        existing_permissions = None
        for permissions in acl['resourceAccess']:
            if 'principalId' in permissions and permissions['principalId'] == principalId:
                existing_permissions = permissions
        if existing_permissions:
            existing_permissions['accessType'] = accessType
        else:
            acl['resourceAccess'].append({u'accessType': accessType, u'principalId': principalId})
        acl = self._storeACL(entity, acl)

        return acl



    ############################################################
    # Provenance
    ############################################################

    ## TODO rename these to Activity
    def getProvenance(self, entity, version=None):
        """
        Retrieve provenance information for a Synapse Entity.
        
        :param entity:  An Entity or Synapse ID to lookup
        :param version: The version of the Entity to retrieve.  
                        Gets the most recent version if omitted
        
        :returns: An Activity object or 
                  raises exception if no provenance record exists
        """

        ## get versionNumber from entity, if it's there
        if version is None and 'versionNumber' in entity:
            version = entity['versionNumber']

        if version:
            uri = '/entity/%s/version/%d/generatedBy' % (id_of(entity), version)
        else:
            uri = '/entity/%s/generatedBy' % id_of(entity)
        return Activity(data=self.restGET(uri))


    def setProvenance(self, entity, activity):
        """
        TODO_Sphinx
        
        :param entity:   An Entity or Synapse ID to modify
        :param activity: TODO_Sphinx
        
        :returns: An updated Activity object
        """
        
        # Assert that the entity was generated by a given Activity.
        if 'id' in activity:
            ## we're updating provenance
            uri = '/activity/%s' % activity['id']
            activity = Activity(data=self.restPUT(uri, json.dumps(activity)))
        else:
            activity = self.restPOST('/activity', body=json.dumps(activity))

        # assert that an entity is generated by an activity
        uri = '/entity/%s/generatedBy?generatedBy=%s' % (id_of(entity), activity['id'])
        activity = Activity(data=self.restPUT(uri))

        return activity


    def deleteProvenance(self, entity):
        """
        Removes provenance information from an Entity 
        and deletes the associated Activity.
        """

        activity = self.getProvenance(entity)
        if not activity: return

        uri = '/entity/%s/generatedBy' % id_of(entity)
        self.restDELETE(uri)

        #TODO: what happens if the activity is shared by more than one entity?

        # delete /activity/{activityId}
        uri = '/activity/%s' % activity['id']
        self.restDELETE(uri)


    def updateActivity(self, activity):
        """
        Modifies an existing Activity.
        
        :returns: An updated Activity object
        """
        uri = '/activity/%s' % activity['id']
        return Activity(data=self.restPUT(uri, json.dumps(activity)))



    ############################################################
    # locationable upload / download
    ############################################################

    def _uploadFileAsLocation(self, entity, filename):
        # TODO - Change this to internal comment style
        # Given an entity or the id of an entity, upload a filename as the location of that entity.
        # 
        # (deprecated in favor of FileEntities)
        # 
        # Arguments:
        # - `entity`:  an entity (dictionary) or Id of entity whose location you want to set 
        # - `filename`: Name of file to upload
        # 
        # Returns:
        # A dictionary with locations (a list of length 1) and the md5 of the file.
        # compute hash of file to be uploaded
        md5 = utils.md5_for_file(filename)

        # guess mime-type - important for confirmation of MD5 sum by receiver
        (mimetype, enc) = mimetypes.guess_type(filename, strict=False)
        if (mimetype is None):
            mimetype = "application/octet-stream"

        # ask synapse for a signed URL for S3 upload
        headers = { 'sessionToken': self.sessionToken,
                    'Content-Type': 'application/json',
                    'Accept': 'application/json' }

        (_, base_filename) = os.path.split(filename)
        data = {'md5':md5.hexdigest(), 'path':base_filename, 'contentType':mimetype}
        uri = '/entity/%s/s3Token' % id_of(entity)
        response_json = self.restPOST(uri, body=json.dumps(data))
        location_path = response_json['path']

        # PUT file to S3
        headers = { 'Content-MD5' : base64.b64encode(md5.digest()),
                    'Content-Type' : mimetype,
                    'x-amz-acl' : 'bucket-owner-full-control' }
        with open(filename, 'rb') as f:
            response = requests.put(response_json['presignedUrl'], headers=headers, data=f)
        response.raise_for_status()

        # Add location to entity. Path will get converted to a signed S3 URL.
        locations = [{'path': location_path, 'type': 'awss3'}]

        return {'locations':locations, 'md5':md5.hexdigest()}


    def _downloadLocations(self, entity):
        # Download files from Locationable entities.
        # Locationable entities contain a signed S3 URL. These URLs expire after a time, so
        # the entity object passed to this method must have been recently acquired from Synapse.
        # (**deprecated** in favor of FileEntities)
        
        if 'locations' not in entity or len(entity['locations'])==0:
            return entity
        location = entity['locations'][0]  #TODO verify that this doesn't fail for unattached files
        url = location['path']
        parseResult = urlparse.urlparse(url)
        pathComponents = parseResult.path.split('/')
        filename = os.path.join(self.cacheDir, entity['id'] ,pathComponents[-1])
        if os.path.exists(filename):
            #print filename, "cached"
            md5str = None
            #see if the MD5 has previously been computed, and that the cached MD5 was done after the file was created
            if os.path.exists( filename + ".md5" ) and os.path.getmtime(filename + ".md5") > os.path.getmtime(filename):
                if self.debug: print "Using Cached MD5"
                handle = open(filename + ".md5")
                md5str = handle.readline().rstrip()
                handle.close()
            else:
                md5str = utils.md5_for_file(filename).hexdigest()
                handle = open(filename + ".md5", "w")
                handle.write(md5str)
                handle.close()

            if md5str != entity.get('md5', ''):
                if self.debug: print filename, "changed, redownloading"
                utils.download_file(url, filename)
        else:
            if self.debug: print filename, 'downloading...',
            utils.download_file(url, filename)

        entity.path=filename
        if entity['contentType']=='application/zip':
            ## Unpack file
            filepath=os.path.join(os.path.dirname(filename), os.path.basename(filename)+'_unpacked')
            #TODO!!!FIX THIS TO BE PATH SAFE!  DON'T ALLOW ARBITRARY UNZIPING
            z = zipfile.ZipFile(filename, 'r')
            z.extractall(filepath) #WARNING!!!NOT SAFE
            ## TODO fix - adding entries for 'files' and 'cacheDir' into entities causes an error in updateEntity
            entity['cacheDir'] = filepath
            entity['files'] = z.namelist()
        else:
            ## TODO fix - adding entries for 'files' and 'cacheDir' into entities causes an error in updateEntity
            entity['cacheDir'] = os.path.dirname(filename)
            entity['files'] = [os.path.basename(filename)]
        return entity



    ############################################################
    # File handle service calls
    ############################################################

    def _downloadFileEntity(self, entity):
        # Download the file associated with a FileEntity
        
        if 'versionNumber' in entity:
            url = '%s/entity/%s/version/%s/file' % (self.repoEndpoint, id_of(entity), entity['versionNumber'])
        else:
            url = '%s/entity/%s/file' % (self.repoEndpoint, id_of(entity),)
        destDir = os.path.join(self.cacheDir, id_of(entity))

        #create destDir if it does not exist
        try:
            os.makedirs(destDir, mode=0700)
        except OSError as exception:
            if exception.errno != os.errno.EEXIST:
                raise
        return self._downloadFile(url, destDir)


    def _downloadFile(self, url, destDir):
        # Download a file from a URL to a local destination directory

        ## we expect to be redirected to a signed S3 URL
        response = requests.get(url, headers=self.headers, allow_redirects=False)
        if response.status_code in [301,302,303,307,308]:
            url = response.headers['location']

            if self.debug:
                print "_downloadFile: redirect url=", url

            ## if it's a file URL, turn it into a path and return it
            if url.startswith('file:'):
                return utils.file_url_to_path(url, verify_exists=True)

            headers = {'sessionToken':self.sessionToken}
            response = requests.get(url, headers=headers, stream=True)
            response.raise_for_status()
        else:
            response.raise_for_status()

        ##Extract filename from url or header, if it is a Signed S3 URL do...
        if url.startswith('https://s3.amazonaws.com/proddata.sagebase.org'):
            filename = utils.extract_filename(response.headers['content-disposition'])
        else:
            filename = url.split('/')[-1]

        ## stream file to disk
        path = os.path.abspath(os.path.join(destDir, filename))
        with open(path, "wb") as f:
            data = response.raw.read(FILE_BUFFER_SIZE)
            while data:
                f.write(data)
                data = response.raw.read(FILE_BUFFER_SIZE)

        return {
            'path': path,
            'files': [os.path.basename(path)],
            'cacheDir': destDir }


    def _uploadToFileHandleService(self, filename, synapseStore=None):
        # Create and return a fileHandle, by either uploading a local file or
        # linking to an external URL.
        # 
        # synapseStore: store file in Synapse or just a URL
        
        if not filename:
            raise ValueError('No filename given')

        elif utils.is_url(filename):
            ## implement downloading and storing remote files? by default??
            # if synapseStore==True:
            #     ## download the file locally, then upload it and cache it?
            #     raise Exception('not implemented, yet!')
            # else:
            #     return self._addURLtoFileHandleService(filename)
            return self._addURLtoFileHandleService(filename)

        ## for local files, we default to uploading the fill unless explicitly instructed otherwise
        else:
            if synapseStore==False:
                return self._addURLtoFileHandleService(filename)
            else:
                return self._chunkedUploadFile(filename)

    def _uploadFileToFileHandleService(self, filepath):
        # Upload a file to the new fileHandle service (experimental)
        # returns a fileHandle which can be used to create a FileEntity or attach to a wiki
           
        #print "_uploadFileToFileHandleService - filepath = " + str(filepath)
        url = "%s/fileHandle" % (self.fileHandleEndpoint,)
        headers = {'Accept': 'application/json', 'sessionToken': self.sessionToken}
        with open(filepath, 'rb') as f:
            response = requests.post(url, files={os.path.basename(filepath): f}, headers=headers)
        response.raise_for_status()

        ## we expect a list of FileHandles of length 1
        fileHandleList = response.json()
        return fileHandleList['list'][0]

    def _addURLtoFileHandleService(self, externalURL):
        # Create a new FileHandle representing an external URL.
        
        fileName = externalURL.split('/')[-1]
        externalURL = utils.as_url(externalURL)
        fileHandle={'concreteType':'org.sagebionetworks.repo.model.file.ExternalFileHandle',
                    'fileName': fileName,
                    'externalURL':externalURL}
        (mimetype, enc) = mimetypes.guess_type(externalURL, strict=False)
        if mimetype:
            fileHandle['contentType'] = mimetype
        return self.restPOST('/externalFileHandle', json.dumps(fileHandle), self.fileHandleEndpoint)

    def _getFileHandle(self, fileHandle):
        # Retrieve a fileHandle from the fileHandle service (experimental).
        
        uri = "/fileHandle/%s" % (id_of(fileHandle),)
        return self.restGET(uri, endpoint=self.fileHandleEndpoint)

    def _deleteFileHandle(self, fileHandle):
        uri = "/fileHandle/%s" % (id_of(fileHandle),)
        self.restDELETE(uri, endpoint=self.fileHandleEndpoint)
        return fileHandle

    def _createChunkedFileUploadToken(self, filepath, mimetype):
        md5 = utils.md5_for_file(filepath)

        chunkedFileTokenRequest = {
            'fileName':os.path.basename(filepath),
            'contentType':mimetype,
            'contentMD5':md5.hexdigest()
        }
        return self.restPOST('/createChunkedFileUploadToken', json.dumps(chunkedFileTokenRequest), endpoint=self.fileHandleEndpoint)

    def _createChunkedFileUploadChunkURL(self, chunkNumber, chunkedFileToken):
        chunkRequest = {'chunkNumber':chunkNumber, 'chunkedFileToken':chunkedFileToken}
        return self.restPOST('/createChunkedFileUploadChunkURL', json.dumps(chunkRequest), endpoint=self.fileHandleEndpoint)

    def _startCompleteUploadDaemon(self, chunkedFileToken, chunkNumbers):
        completeAllChunksRequest = {'chunkNumbers': chunkNumbers,
                                    'chunkedFileToken': chunkedFileToken}
        return self.restPOST('/startCompleteUploadDaemon', json.dumps(completeAllChunksRequest), endpoint=self.fileHandleEndpoint)

    def _completeUploadDaemonStatus(self, status):
        return self.restGET('/completeUploadDaemonStatus/%s' % status['daemonId'], endpoint=self.fileHandleEndpoint)

    def _chunkedUploadFile(self, filepath, chunksize=CHUNK_SIZE, verbose=False, progress=True):
        # Upload a file to be stored in Synapse, dividing large files into chunks.
        # 
        # filepath: the file to be uploaded
        # chunksize: chop the file into chunks of this many bytes. The default value is
        #            5MB, which is also the minimum value.
        # 
        # returns an S3FileHandle
        
        if chunksize < 5*MB:
            raise ValueError('Minimum chunksize is 5 MB.')
        if filepath is None or not os.path.exists(filepath):
            raise ValueError('File not found: ' + str(filepath))
    
        old_debug = self.debug
        if verbose=='debug':
            self.debug = True

        ## start timing
        start_time = time.time()

        try:
            i = 0

            # guess mime-type - important for confirmation of MD5 sum by receiver
            (mimetype, enc) = mimetypes.guess_type(filepath, strict=False)
            if (mimetype is None):
                mimetype = "application/octet-stream"

            ## S3 wants 'content-type' and 'content-length' headers. S3 doesn't like
            ## 'transfer-encoding': 'chunked', which requests will add for you, if it
            ## can't figure out content length. The errors given by S3 are not very
            ## informative:
            ## If a request mistakenly contains both 'content-length' and
            ## 'transfer-encoding':'chunked', you get [Errno 32] Broken pipe.
            ## If you give S3 'transfer-encoding' and no 'content-length', you get:
            ##   501 Server Error: Not Implemented
            ##   A header you provided implies functionality that is not implemented
            headers = { 'Content-Type' : mimetype}

            ## get token
            token = self._createChunkedFileUploadToken(filepath, mimetype)
            if verbose: sys.stderr.write('\n\ntoken= ' + str(token) + '\n')

            if progress:
                sys.stdout.write('.')
                sys.stdout.flush()

            ## define the retry policy for uploading chunks
            with_retry = RetryRequest(
                retry_status_codes=[502,503],
                retry_errors=['We encountered an internal error. Please try again.'],
                retries=4, wait=1, back_off=2, verbose=verbose,
                tag='S3 put RetryRequest')

            with open(filepath, 'rb') as f:
                for chunk in utils.chunks(f, chunksize):
                    i += 1
                    if verbose: sys.stderr.write('\nChunk %d\n' % i)

                    ## get the signed S3 URL
                    url = self._createChunkedFileUploadChunkURL(i, token)
                    if verbose: sys.stderr.write('url= ' + str(url) + '\n')
                    if progress:
                        sys.stdout.write('.')
                        sys.stdout.flush()

                    ## PUT the chunk to S3
                    response = with_retry(requests.put)(url, data=chunk, headers=headers)
                    response.raise_for_status()
                    if progress:
                        sys.stdout.write(',')
                        sys.stdout.flush()

                    ## Is requests closing response stream? Let's make sure:
                    ## "Note that connections are only released back to
                    ##  the pool for reuse once all body data has been
                    ##  read; be sure to either set stream to False or
                    ##  read the content property of the Response object."
                    ## see: http://docs.python-requests.org/en/latest/user/advanced/#keep-alive
                    try:
                        if response:
                            throw_away = response.content
                    except Exception as ex:
                        sys.stderr.write('error reading response: '+str(ex))

            status = self._startCompleteUploadDaemon(chunkedFileToken=token, chunkNumbers=[a+1 for a in range(i)])

            ## poll until concatenating chunks is complete
            while (status['state']=='PROCESSING'):
                if progress:
                    sys.stdout.write('!')
                    sys.stdout.flush()
                time.sleep(CHUNK_UPLOAD_POLL_INTERVAL)
                status = self._completeUploadDaemonStatus(status)
                if verbose: sys.stderr.write('status= ' + str(status) + '\n')
                #sys.stderr.write(str(status['runTimeMS']) + '\t' + str(status['percentComplete']) + '\n')

            if progress:
                sys.stdout.write('!\n')
                sys.stdout.flush()

            if status['state']=='FAILED':
                raise Exception(status['errorMessage'])

            ## return a fileHandle
            fileHandle = self._getFileHandle(status['fileHandleId'])

            ## print timing information
            if progress: sys.stdout.write("Upload completed in %s.\n" % utils.format_time_interval(time.time()-start_time))

            return fileHandle
        finally:
            self.debug = old_debug


    ############################################################
    # Summary methods
    ############################################################

    def _traverseTree(self, id, name=None, version=None):
        children =  self.query("select id, versionNumber, name from entity where entity.parentId=='%s'" %id)
        count=children['totalNumberOfResults']
        print id, count, name
        children=children['results']
        output=[]
        if count>0:
            output.append({'name':name, 'targetVersionNumber':version, 'targetId':id, 'records':[]})
            for ent in children:
                output[-1]['records'].extend(self._traverseTree(ent['entity.id'], ent['entity.name'], ent['entity.versionNumber']))
        else:
            output.append({'targetId':id, 'targetVersionNumber':version})
        return output

    def _flattenTree2Groups(self,tree, level=0, out=[]):
        # Converts a complete tree to 2 levels corresponding to json schema of summary
        # 
        # Arguments:
        # - `tree`: json object representing entity organizion as output from _traverseTree
        if level==0:  #Move direct entities to subgroup "Content"
            #I am so sorry!  This is incredibly inefficient but I had no time to think through it.
            contents = [group for group in tree if not group.has_key('records')]
            tree.append({'name':'Content', 'records':contents, 'targetId':'', 'targetVersionNumber':''})
            for i in sorted([i for i, group in enumerate(tree) if not group.has_key('records')], reverse=True):
                tree.pop(i)

            #tree=[group for i, group in enumerate(tree) if i not in contents]
            self.printEntity(tree)
            print "============================================"
        
        for i, group in enumerate(tree):
            if group.has_key('records'): #Means that it has subrecords
                self._flattenTree2Groups(group['records'], level+1, out)
            else:
                out.append({'entityReference':group})
            if level==0:
                del group['targetId']
                del group['targetVersionNumber']
                group['records']=out
                out=list()

    def createSnapshotSummary(self, id, name='summary', description=None, groupBy=None ):
        """
        Traverses all sub-Entities of the given Entity 
        and creates a summary object within the given Entity.
        :param id:          Id of Entity to traverse to create Entity 
        :param name:        Name of created summary Entity
        :param description: Description of created Entity
        """
        print "hello"
        tree=self._traverseTree(id)[0]['records']
        print self.printEntity(tree)
        
        #TODO: Instead of doing a flatten just by the default hierarchy structure I should be 
        #using an external group-by parameter that determines weather by what property of structure
        # to group by.
        self._flattenTree2Groups(tree)
        self.printEntity(tree)
        self.createEntity({'name': name,
                           "description": description,
                           "entityType": "org.sagebionetworks.repo.model.Summary", 
                           "groups": tree,
                           "parentId": id})



    ############################################################
    # CRUD for Evaluations
    ############################################################

    def getEvaluation(self, id):
        """Gets an Evaluation object from Synapse."""
        
        evaluation_id = id_of(id)
        uri=Evaluation.getURI(id)
        return Evaluation(**self.restGET(uri))


    def submit(self, evaluation, entity, name=''):
        """
        Submit an Entity for evaluation by an evaluator.
        
        :param entity:     The Entity containing the Submission
        :param evaluation: Evaluation board to submit to
        
        :returns: A Submission object
        """

        evaluation_id = id_of(evaluation)
        if not 'versionNumber' in entity:
            entity = self.get(entity)    
        entity_version = entity['versionNumber']
        entity_id = entity['id']

        name = entity['name'] if (name == '' and 'name' in entity) else ''
        submission =  {'evaluationId':evaluation_id, 'entityId':entity_id, 'name':name, 
                       'versionNumber': entity_version}
        return Submission(**self.restPOST('/evaluation/submission?etag=%s' %entity.etag, 
                                        json.dumps(submission)))


    def addEvaluationParticipant(self, evaluation, userId=None):
        """
        Adds a participant to an Evaluation.

        :param evaluation: An Evaluation object or Evaluation ID
        :param userId:     The prinicipal ID of the participant.
                           If not supplied, uses your ID
        """
        
        evaluation_id = id_of(evaluation)
        userId=self.getUserProfile()['ownerId'] if userId==None else userId
        self.restPOST('/evaluation/%s/participant/%s'  %(evaluation_id, userId), {})


    def getSubmissions(self, evaluation, status=None):
        """
        TODO_Sphinx
        
        :param evaluation: Evaluation board to get submissions from.
        :param status:     Get submissions that have specific status 
                           one of {OPEN, CLOSED, SCORED, INVALID}
                           
        :returns: A generator over all Submissions for an Evaluation, 
                  optionally filters Submissions for a specified status.
                  
        Example::
        
            for submission in syn.getEvaluationSubmissions(1234567):
                print submission['entityId']
        """
        
        evaluation_id = evaluation['id'] if 'id' in evaluation else str(evaluation)

        result_count = 0
        limit = 20
        offset = 0 - limit
        max_results = 1000 ## gets updated later
        submissions = []

        while result_count < max_results:
            ## if we're out of results, do a(nother) REST call
            if result_count >= offset + len(submissions):
                offset += limit
                if status != None:
                    if status not in ['OPEN', 'CLOSED', 'SCORED', 'INVALID']:
                        raise Exception('status may be one of {OPEN, CLOSED, SCORED, INVALID}')
                    uri = "/evaluation/%s/submission/all?status=%s&limit=%d&offset=%d" %(evaluation_id, 
                                                                                         status, limit, 
                                                                                         offset)
                else:
                    uri = "/evaluation/%s/submission/all?limit=%d&offset=%d" %(evaluation_id, limit, 
                                                                               offset)
                page_of_submissions = self.restGET(uri)
                max_results = page_of_submissions['totalNumberOfResults']
                submissions = page_of_submissions['results']
                if len(submissions)==0:
                    return

            i = result_count - offset
            result_count += 1
            yield Submission(**submissions[i])


    def getOwnSubmissions(self, evaluation):
        """
        TODO_Sphinx - Not implemented yet
        """
        
        #TODO implement this if this is really usefull?
        pass


    def getSubmission(self, id):
        """Gets a Submission object."""
        
        submission_id = id_of(id)
        uri=SubmissionStatus.getURI(submission_id)
        return Submission(**self.restGET(uri))


    def getSubmissionStatus(self, submission):
        """
        Downloads the status of a Submission.
        
        :param submission: The Submission to lookup
        
        :returns: A SubmissionStatus object
        """
        submission_id = id_of(submission)
        uri=SubmissionStatus.getURI(submission_id)
        val=self.restGET(uri)
        return SubmissionStatus(**val)


            

    ############################################################
    # CRUD for Wikis
    ############################################################

    def getWiki(self, owner, subpageId=None):
        """Gets a Wiki object from Synapse."""
        owner_type = utils.guess_object_type(owner)
        if subpageId:
            uri = '/%s/%s/wiki/%s' % (owner_type, id_of(owner), id_of(subpageId))
        else:
            uri = '/%s/%s/wiki' % (owner_type, id_of(owner))
        wiki = self.restGET(uri)
        wiki['owner'] = owner
        return Wiki(**wiki)
        

    def getWikiHeaders(self, owner):
        """Retrieves the the header of all Wiki's belonging to the owner."
        
        :param owner: An Evaluation or Entity
        
        :returns: TODO_Sphinx
        """
        owner_type = utils.guess_object_type(owner)
        uri = '/%s/%s/wikiheadertree' % (owner_type, id_of(owner),)
        return self.restGET(uri)

    #Need to test functionality of this
    # def _downloadWikiAttachment(self, owner, wiki, filename, dest_dir=None, owner_type=None):
    #     # Download a file attached to a wiki page
    #     if not owner_type:
    #         owner_type = utils.guess_object_type(owner)
    #     url = "%s/%s/%s/wiki/%s/attachment?fileName=%s" % (self.repoEndpoint, owner_type, id_of(owner), id_of(wiki), filename,)
    #     return self._downloadFile(url, dest_dir)

    ##Superseded by getWiki
    # def _createWiki(self, owner, title, markdown, attachmentFileHandleIds=None, owner_type=None):
    #     # Create a new wiki page for an Entity (experimental).
    #     # parameters:
    #     # owner -- the owner object (entity, competition, evaluation) with which the new wiki page will be associated
    #     # markdown -- the contents of the wiki page in markdown
    #     # attachmentFileHandleIds -- a list of file handles or file handle IDs
    #     # owner_type -- entity, competition, evaluation, can usually be automatically inferred from the owner object
    # 
    #     if not owner_type:
    #         owner_type = utils.guess_object_type(owner)
    #     uri = '/%s/%s/wiki' % (owner_type, id_of(owner),)
    #     wiki = {'title':title, 'markdown':markdown}
    #     if attachmentFileHandleIds:
    #         wiki['attachmentFileHandleIds'] = attachmentFileHandleIds
    #     return self.restPOST(uri, body=json.dumps(wiki))


    ############################################################
    # CRUD for Entities (properties)
    ############################################################

    def _getEntity(self, entity, version=None):
        # Get an entity from Synapse.
        # entity: a Synapse ID, a dictionary representing an entity or a Synapse Entity object
        # returns a dictionary representing an entity, specifically it's properties
        
        uri = '/entity/'+id_of(entity)
        if version:
            uri += '/version/%d' % version
        return self.restGET(uri=uri)

    def _createEntity(self, entity):
        # Create a new entity in Synapse.
        # entity: a dictionary representing an entity or a Synapse Entity object
        # returns a dictionary representing an entity, specifically it's properties
        
        if self.debug: print "\n\n~~~ creating ~~~\n" + json.dumps(get_properties(entity), indent=2)
        return self.restPOST(uri='/entity', body=json.dumps(get_properties(entity)))

    def _updateEntity(self, entity, incrementVersion=True, versionLabel=None):
        # Update an existing entity in Synapse.
        # entity: a dictionary representing an entity or a Synapse Entity object
        # returns a dictionary representing an entity, specifically it's properties
        
        uri = '/entity/%s' % id_of(entity)

        if is_versionable(entity):
            if incrementVersion:
                uri += '/version'
                if 'versionNumber' in entity:
                    entity['versionNumber'] += 1
                    if 'versionLabel' in entity:
                        entity['versionLabel'] = str(entity['versionNumber'])

        if versionLabel:
            entity['versionLabel'] = str(versionLabel)

        if self.debug: print "\n\n~~~ updating ~~~\n" + json.dumps(get_properties(entity), indent=2)
        return self.restPUT(uri=uri, body=json.dumps(get_properties(entity)))

    def _findEntityIdByNameAndParent(self, name, parent=ROOT_ENTITY):
        # Find an entity given its name and parent or parentId
        
        qr = self.query('select * from entity where name=="%s" and parentId=="%s"' % (name, id_of(parent),))
        if qr.get('totalNumberOfResults',None) == 1:
            return qr['results'][0]['entity.id']
        else:
            return None


    ############################################################
    # Low level Rest calls
    ############################################################
    @STANDARD_RETRY_REQUEST
    def restGET(self, uri, endpoint=None, **kwargs):
        """
        Performs a REST GET operation to the Synapse server.
        
        :param uri:      URI on which get is performed
        :param endpoint: Server endpoint, defaults to self.repoEndpoint
        :param kwargs:   Any other arguments taken by a `requests <http://docs.python-requests.org/en/latest/>`_ method

        :returns: JSON encoding of response
        """
        if endpoint==None:
            endpoint=self.repoEndpoint    
        response = requests.get(endpoint+uri, headers=self.headers, **kwargs)
        self._storeTimingProfile(response)
        if self.debug:
            utils.debug_response(response)
        try:
            response.raise_for_status()
        except:
            sys.stderr.write(response.content+'\n')
            raise
        return response.json()
     
    @STANDARD_RETRY_REQUEST
    def restPOST(self, uri, body, endpoint=None, **kwargs):
        """
        Performs a REST POST operation to the Synapse server.
        
        :param uri:      URI on which get is performed
        :param endpoint: Server endpoint, defaults to self.repoEndpoint
        :param body:     The payload to be delivered 
        :param kwargs:   Any other arguments taken by a `requests <http://docs.python-requests.org/en/latest/>`_ method

        :returns: JSON encoding of response

        """
        if endpoint==None:
            endpoint=self.repoEndpoint    
        response = requests.post(endpoint + uri, data=body, headers=self.headers, **kwargs)
        if self.debug:
            utils.debug_response(response)
        try:
            response.raise_for_status()
        except:
            sys.stderr.write(response.content+'\n')
            raise

        if response.headers.get('content-type',None) == 'application/json':
            return response.json()
        # 'content-type': 'text/plain;charset=ISO-8859-1'
        return response.text

    @STANDARD_RETRY_REQUEST
    def restPUT(self, uri, body=None, endpoint=None, **kwargs):
        """
        Performs a REST PUT operation to the Synapse server.
        
        :param uri:      URI on which get is performed
        :param endpoint: Server endpoint, defaults to self.repoEndpoint
        :param body:     The payload to be delivered 
        :param kwargs:   Any other arguments taken by a `requests <http://docs.python-requests.org/en/latest/>`_ method

        :returns: JSON encoding of response
        """
        if endpoint==None:
            endpoint=self.repoEndpoint    
        response = requests.put(endpoint + uri, data=body, headers=self.headers, **kwargs)
        if self.debug:
            utils.debug_response(response)
        try:
            response.raise_for_status()
        except:
            sys.stderr.write(response.content+'\n')
            raise
            
        if response.headers.get('content-type',None) == 'application/json':
            return response.json()
        return response.text

    @STANDARD_RETRY_REQUEST
    def restDELETE(self, uri, endpoint=None, **kwargs):
        """
        Performs a REST DELETE operation to the Synapse server.
        
        :param uri:      URI of resource to be deleted
        :param endpoint: Server endpoint, defaults to self.repoEndpoint
        :param kwargs:   Any other arguments taken by a `requests <http://docs.python-requests.org/en/latest/>`_ method

        """
        if endpoint==None:
            endpoint=self.repoEndpoint    
        response = requests.delete(endpoint+uri, headers=self.headers, **kwargs)
        if self.debug:
            utils.debug_response(response)
        try:
            response.raise_for_status()
        except:
            sys.stderr.write(response.content+'\n')
            raise


