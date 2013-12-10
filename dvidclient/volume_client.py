from httplib import HTTPConnection
import threading
import contextlib
import StringIO
import json

import numpy

from volume_metainfo import MetaInfo
from volume_codec import VolumeCodec

import logging
logger = logging.getLogger(__name__)

class VolumeClient(object):
    """
    Http client for retrieving a cutout volume from a DVID server.
    An instance of VolumeClient is capable of retrieving data from only one remote data volume.
    To retrieve data from multiple remote volumes, instantiate multiple VolumeClient objects.
    """
    
    class ErrorResponseException( Exception ):
        def __init__(self, attempted_action, status_code, reason, response_body):
            self.attempted_action = attempted_action
            self.status_code = status_code
            self.reason = reason
            self.response_body = response_body
        
        def __str__(self):
            caption = 'While attempting "{}" DVID returned an error: {}, "{}"\n'\
                      ''.format( self.attempted_action, self.status_code, self.reason )
            if self.status_code == 500:
                caption += "Server response body:\n"
                caption += self.response_body
            return caption

    @classmethod
    def create_volume(cls, hostname, uuid, data_name, metainfo):
        """
        Class method.
        Open a connection to the server and create a new remote volume.
        After creating the volume, you can instantiate a new VolumeClient to access it.
        """
        with contextlib.closing( HTTPConnection(hostname) ) as connection:
            dvid_typename = metainfo.determine_dvid_typename()
            rest_query = "/api/dataset/{uuid}/new/{dvid_typename}/{data_name}"\
                         "".format( **locals() )
            metainfo_json = metainfo.format_to_json()
            headers = { "Content-Type" : "text/json" }
            connection.request( "POST", rest_query, body=metainfo_json, headers=headers )
    
            with contextlib.closing( connection.getresponse() ) as response:
                if response.status != 204:
                    raise VolumeClient.ErrorResponseException( 
                        "create new data", response.status, response.reason, response.read() )
                response_text = response.read()
                if response_text:
                    raise Exception( "Expected an empty response from the DVID server.  "
                                     "Got: {}".format( response_text ) )

    @classmethod
    def query_datasets_info(cls, hostname):
        """
        Query DVID for the list of datasets and the associated 
        nodes and data items within each node.
        """
        with contextlib.closing( HTTPConnection(hostname) ) as connection:
            rest_query = "/api/datasets/info"
            connection.request( "GET", rest_query )
            with contextlib.closing( connection.getresponse() ) as response:
                if response.status != 200:
                    raise VolumeClient.ErrorResponseException( 
                        "query datasets info", response.status, response.reason, response.read() )
                
                try:
                    datasets_info = json.loads( response.read() )
                except ValueError as ex:
                    raise Exception( "Couldn't parse the dataset info response as json:\n"
                                     "{}".format( ex.args ) )
                
                # TODO: Schema validation
                return datasets_info

    def __init__(self, hostname, uuid, data_name):
        """
        hostname: The DVID server hostname
        uuid: The node uuid
        data_name: The name of the volume
        """
        # Open a connection to the server
        self.hostname = hostname
        self.uuid = uuid
        self.data_name = data_name
        connection = HTTPConnection(hostname)
        self._connection = connection
        rest_query = "/api/node/{uuid}/{data_name}/schema".format( uuid=uuid, data_name=data_name )
        connection.request( "GET", rest_query )
        
        response = connection.getresponse()
        if response.status != 200:
            raise self.ErrorResponseException( 
                "metainfo query", response.status, response.reason, response.read() )

        self.metainfo = MetaInfo.create_from_json( response.read() )
        self._codec = VolumeCodec( self.metainfo )
        
        self._lock = threading.Lock() # TODO: Instead of locking, auto-instantiate separate connections for each thread...
    
    def retrieve_subvolume(self, start, stop):
        """
        Retrieve a subvolume from the remote server.
        start, stop: The start and stop coordinates of the region to retrieve.
                     Must include all axes of the dataset.
        """
        rest_query = self._format_subvolume_rest_query(start, stop)
        # TODO: Instead of locking, auto-instantiate separate connections for each thread...
        with self._lock:
            self._connection.request( "GET", rest_query )
            with contextlib.closing( self._connection.getresponse() ) as response:
                if response.status != 200:
                    raise self.ErrorResponseException( 
                        "subvolume query", response.status, response.reason, response.read() )
                
                # "Full" roi shape includes channel axis and ALL channels
                full_roi_shape = numpy.array(stop) - start
                full_roi_shape[0] = self.metainfo.shape[0]
                vdata = self._codec.decode_to_vigra_array( response, full_roi_shape )
    
                # Was the response fully consumed?  Check.
                # NOTE: This last read() is not optional.
                # Something in the http implementation gets upset if we read out the exact amount we needed.
                # That is, we MUST read beyond the end of the stream.  So, here we go. 
                excess_data = response.read()
                if excess_data:
                    # Uh-oh, we expected it to be empty.
                    raise Exception( "Received data was longer than expected by {} bytes.  (Expected only {} bytes.)"
                                     "".format( len(excess_data), len(numpy.getbuffer(vdata)) ) ) 
        # Select the requested channels from the returned data.
        return vdata[start[0]:stop[0]]

    def modify_subvolume(self, start, stop, new_data):
        assert start[0] == 0, "Subvolume modifications must include all channels."
        assert stop[0] == self.metainfo.shape[0], "Subvolume modifications must include all channels."

        rest_query = self._format_subvolume_rest_query(start, stop)
        body_data_stream = StringIO.StringIO()
        self._codec.encode_from_vigra_array(body_data_stream, new_data)
        with self._lock:
            headers = { "Content-Type" : VolumeCodec.VOLUME_MIMETYPE }
            self._connection.request( "POST", rest_query, body=body_data_stream.getvalue(), headers=headers )
            with contextlib.closing( self._connection.getresponse() ) as response:
                if response.status != 204:
                    raise self.ErrorResponseException( 
                        "subvolume post", response.status, response.reason, response.read() )
                
                # Something (either dvid or the httplib) gets upset if we don't read the full response.
                response.read()

    def _format_subvolume_rest_query(self, start, stop):
        start = numpy.asarray(start)
        stop = numpy.asarray(stop)
        shape = self.metainfo.shape

        assert len(start) == len(stop) == len(shape), \
            "start/stop/shape mismatch: {}/{}/{}".format( start, stop, shape )
        assert (start < stop).all(), "Invalid start/stop: {}/{}".format( start, stop )
        assert (start >= 0).all(), "Invalid start: {}".format( start )
        assert (start < shape).all(), "Invalid start/shape: {}/{}".format( start, shape )
        assert (stop <= shape).all(), "Invalid stop/shape: {}/{}".format( stop, shape )

        # Drop channel before requesting from DVID
        channel_index = self.metainfo.axistags.channelIndex
        start = numpy.delete( start, channel_index )
        stop = numpy.delete( stop, channel_index )

        # Dvid roi shape doesn't include channel
        dvid_roi_shape = stop - start
        roi_shape_str = "_".join( map(str, dvid_roi_shape) )
        start_str = "_".join( map(str, start) )
        
        num_dims = len(self.metainfo.shape)
        dims_string = "_".join( map(str, range(num_dims-1) ) )
        rest_query = "/api/node/{uuid}/{data_name}/{dims_string}/{roi_shape_str}/{start_str}"\
                     "".format( uuid=self.uuid, 
                                data_name=self.data_name, 
                                dims_string=dims_string, 
                                roi_shape_str=roi_shape_str, 
                                start_str=start_str )
        return rest_query

