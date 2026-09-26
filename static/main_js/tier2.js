//prevent browser back
history.pushState(null, null, document.URL);
window.addEventListener('popstate', function () {
    history.pushState(null, null, document.URL);
});

//prevent right-click
document.addEventListener("contextmenu", function(e){
  e.preventDefault();
})

const homeURL = 'http://127.0.0.1:5000/';

var userid, usertype, firstname, midname, lastname, email, contact, dob, ssn, address;

function getUser() {
  console.log("gettier2 called");

  //userid = localStorage.getItem('user');
  console.log("userid retrieved from local storage ="+ userid);

  const loadUserData = {
    employee_id : userid,
    usertype : 'tier2'
  };

  fetch(homeURL+'loadEmployee', {
    method : 'post',
    body : JSON.stringify(loadUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("gettier2 response received");
    if (response.redirected) {
      localStorage.setItem('loggedStatus', '0');
      window.location.href = response.url;
    }
    else {
      return response.json();
    }
  }).then(function (data) {
    console.log(data);
    appendPrimaryData(data);
  }).catch(function(error){
    console.error(error);
  });
}

function appendPrimaryData(data) {
  first_name = data.Info.first_name;
  midname = data.Info.middle_name;
  lastname = data.Info.last_name;
  email = data.Info.email_id;
  contact = data.Info.contact_no;
  dob = data.Info.dob;
  ssn = data.Info.ssn;
  address = data.Info.address;

  document.getElementById("account_first_name").value = first_name;
  document.getElementById("account_middle_name").value = midname;
  document.getElementById("account_last_name").value = lastname;
  document.getElementById("account_email_id").value = email;
  document.getElementById("account_contact_no").value = contact;
  document.getElementById("account_dob").value = dob;
  document.getElementById("account_ssn").value = ssn;
  document.getElementById("account_address").value = address;

  fillPendingTransTbl(data);
}

function fillPendingTransTbl(data){
  var table = document.getElementById('pending_requests_tbl');
  var rowCount = table.rows.length;

  var selection = document.getElementById('customer_trans_no');
  try {
    for(var i=1; i<rowCount; i++) {
      table.deleteRow(i);
      selection.remove(i);
      rowCount--;
      i--;
    }
  }catch(e) {
    alert(e);
  }

  if(data.FundsRequests != 'None') {
    for (var i=0; i< data.FundsRequests.length; i++){
      var rowCount = table.rows.length;
      var row = table.insertRow(rowCount);
      var cell1 = row.insertCell(0);
      cell1.innerHTML = data.FundsRequests[i][0];

      var cell2 = row.insertCell(1);
      cell2.innerHTML = data.FundsRequests[i][1];

      var cell3 = row.insertCell(2);
      cell3.innerHTML = data.FundsRequests[i][2];

      var cell4 = row.insertCell(3);
      cell4.innerHTML = data.FundsRequests[i][5];

      var option = document.createElement("OPTION");
      option.innerHTML = data.FundsRequests[i][0];
      option.value = data.FundsRequests[i][0];
      //Add the Option element to DropDownList.
      selection.options.add(option);
    }
  }
}

function logout() {
  console.log("logout called");

  //userid = localStorage.getItem('user');
  console.log("userid retrieved from local storage ="+ userid);

  const logoutUserData = {
    userid : userid
  };

  fetch(homeURL+'logout', {
    method : 'post',
    body : JSON.stringify(logoutUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("logout response received");
    if (response.redirected) {
      window.location.href = response.url;
    }
    else {
      window.alert("Oops we encountered an error!");
    }
  }).catch(function(error){
    console.error(error);
  });
}

function updateInfo(userid, email_info, contact_info, address_info) {
  console.log("updateInfo called");

  const updateInfoData = {
    userid : userid,
    email : email_info,
    contact_no : contact_info,
    address : address_info,
    requester : 'Employee'
  };

  fetch(homeURL+'updateInfo', {
    method : 'post',
    body : JSON.stringify(updateInfoData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("updateInfo response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Update Info Request Placed'){
      window.alert('Update Info Request Placed!');
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
}

function getCustomer(customer_id) {
  console.log("getcustomer called");

  const loadCustomerData = {
    userid: userid,
    customer_id : customer_id
  };

  fetch(homeURL+'getCustomer', {
    method : 'post',
    body : JSON.stringify(loadCustomerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getcustomer response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    appendSecondaryData(customer_id, data);
  }).catch(function(error){
    console.error(error);
  });
}

function getModifyCustomer(customer_id) {
  console.log("getcustomer called");

  const loadCustomerData = {
    userid: userid,
    customer_id : customer_id
  };

  fetch(homeURL+'getCustomer', {
    method : 'post',
    body : JSON.stringify(loadCustomerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getcustomer response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    document.getElementById("modify_first_name").value = data.Info.first_name;
    document.getElementById("modify_middle_name").value = data.Info.middle_name;
    document.getElementById("modify_last_name").value = data.Info.last_name;
    document.getElementById("modify_email_id").value = data.Info.email_id;
    document.getElementById("modify_contact_no").value = data.Info.contact_no;
    document.getElementById("modify_dob").value = data.Info.dob;
    document.getElementById("modify_ssn").value = data.Info.ssn;
    document.getElementById("modify_address").value = data.Info.address;
  }).catch(function(error){
    console.error(error);
  });
}

function appendSecondaryData(customer_id, data) {

  document.getElementById("customer_id").innerHTML = customer_id;
  document.getElementById("first_name").innerHTML = data.Info.first_name;
  document.getElementById("middle_name").innerHTML = data.Info.middle_name;
  document.getElementById("last_name").innerHTML = data.Info.last_name;
  document.getElementById("email_id").innerHTML = data.Info.email_id;
  document.getElementById("contact_no").innerHTML = data.Info.contact_no;
  document.getElementById("dob").innerHTML = data.Info.dob;
  document.getElementById("ssn").innerHTML = data.Info.ssn;
  document.getElementById("address").innerHTML = data.Info.address;

  fillCustomerAccTbl(data);
  fillStaffLinked(data);
  fillStaffWires(data);
  fillStaffInWires(data);
  if($('#cust_details_card').css('display')=='none'){
    $('#cust_details_card').show();
  }
  if($('#cust_accounts_tbl').css('display')=='none'){
    $('#cust_details_tbl').show();
  }
}

function fillStaffLinked(data) {
  var snapshot = data.LinkedAccounts || {};
  var card = document.getElementById('staff_linked_card');
  if (!card) {
    return;
  }
  card.style.display = 'block';
  var summary = document.getElementById('staff_linked_summary');
  if (summary) {
    summary.innerHTML = 'YTD push: $' + (snapshot.ytd_push || '0.00') +
      ' &middot; YTD pull: $' + (snapshot.ytd_pull || '0.00') +
      ' &middot; returned: $' + (snapshot.returned_ytd || '0.00');
  }
  var table = document.getElementById('staff_linked_tbl');
  var body = table.getElementsByTagName('tbody')[0];
  body.innerHTML = '';
  var linkSelect = document.getElementById('staff_la_link_id');
  linkSelect.options.length = 1;
  var links = snapshot.links || [];
  for (var i = 0; i < links.length; i++) {
    var row = body.insertRow(-1);
    row.insertCell(0).innerHTML = links[i].nickname;
    row.insertCell(1).innerHTML = links[i].method;
    row.insertCell(2).innerHTML = links[i].status;
    row.insertCell(3).innerHTML = (links[i].routing_last4 || '') + '/' + (links[i].account_last4 || '');
    if (links[i].status != 'pending') {
      continue;
    }
    var option = document.createElement('OPTION');
    option.value = links[i].link_id;
    option.innerHTML = links[i].nickname + ' (' + links[i].method + ')';
    linkSelect.options.add(option);
  }
  var moveTable = document.getElementById('staff_linked_movements_tbl');
  var moveBody = moveTable.getElementsByTagName('tbody')[0];
  moveBody.innerHTML = '';
  var selection = document.getElementById('staff_la_movement_id');
  selection.options.length = 1;
  var movements = snapshot.movements || [];
  for (var j = 0; j < movements.length; j++) {
    var prow = moveBody.insertRow(-1);
    prow.insertCell(0).innerHTML = movements[j].nickname + ' ' + movements[j].direction;
    prow.insertCell(1).innerHTML = '$' + movements[j].amount;
    prow.insertCell(2).innerHTML = movements[j].status;
    prow.insertCell(3).innerHTML = movements[j].movement_id.slice(0, 8);
    if (movements[j].status != 'sent') {
      continue;
    }
    var moveOpt = document.createElement('OPTION');
    moveOpt.value = movements[j].movement_id;
    moveOpt.innerHTML = movements[j].nickname + ' $' + movements[j].amount;
    selection.options.add(moveOpt);
  }
}

function postStaffLinked(path, payload) {
  payload.userid = userid;
  fetch(homeURL + path, {
    method: 'post',
    body: JSON.stringify(payload),
    headers: { 'Content-type': 'application/json' }
  }).then(function(response) {
    return response.json().then(function(body) {
      var result = document.getElementById('staff_la_result');
      if (!response.ok) {
        if (result) { result.innerHTML = (body && (body.message || body.error)) || 'Failed'; }
        return;
      }
      if (result) { result.innerHTML = body.message || 'Done'; }
      getCustomer($('#customer_id_input').val() || document.getElementById('customer_id').innerHTML);
    });
  }).catch(function(error) {
    console.error(error);
  });
}

function fillStaffWires(data) {
  var snapshot = data.Wires || {};
  var card = document.getElementById('staff_wire_card');
  if (!card) {
    return;
  }
  card.style.display = 'block';
  var summary = document.getElementById('staff_wire_summary');
  if (summary) {
    summary.innerHTML = 'YTD sent: $' + (snapshot.ytd_sent || '0.00') +
      ' &middot; fees: $' + (snapshot.ytd_fees || '0.00') +
      ' &middot; recalled: $' + (snapshot.recalled_ytd || '0.00');
  }
  var beneTable = document.getElementById('staff_wire_bene_tbl');
  var beneBody = beneTable.getElementsByTagName('tbody')[0];
  beneBody.innerHTML = '';
  var benes = snapshot.beneficiaries || [];
  for (var i = 0; i < benes.length; i++) {
    var row = beneBody.insertRow(-1);
    row.insertCell(0).innerHTML = benes[i].nickname;
    row.insertCell(1).innerHTML = benes[i].legal_name;
    row.insertCell(2).innerHTML = benes[i].aba;
    row.insertCell(3).innerHTML = benes[i].status;
  }
  var table = document.getElementById('staff_wire_tbl');
  var body = table.getElementsByTagName('tbody')[0];
  body.innerHTML = '';
  var selection = document.getElementById('staff_wire_id');
  selection.options.length = 1;
  var wires = snapshot.wires || [];
  for (var j = 0; j < wires.length; j++) {
    var wrow = body.insertRow(-1);
    wrow.insertCell(0).innerHTML = wires[j].nickname;
    wrow.insertCell(1).innerHTML = '$' + wires[j].amount;
    wrow.insertCell(2).innerHTML = wires[j].status;
    wrow.insertCell(3).innerHTML = (wires[j].imad || wires[j].wire_id).slice(0, 12);
    if (['held', 'queued', 'pending_release', 'sent'].indexOf(wires[j].status) < 0) {
      continue;
    }
    var option = document.createElement('OPTION');
    option.value = wires[j].wire_id;
    option.innerHTML = wires[j].nickname + ' $' + wires[j].amount + ' (' + wires[j].status + ')';
    selection.options.add(option);
  }
}

function fillStaffInWires(data) {
  var snapshot = data.InWires || {};
  var card = document.getElementById('staff_inwire_card');
  if (!card) {
    return;
  }
  card.style.display = 'block';
  var summary = document.getElementById('staff_inwire_summary');
  if (summary) {
    summary.innerHTML = 'YTD posted: $' + (snapshot.ytd_posted || '0.00') +
      ' &middot; returned: $' + (snapshot.ytd_returned || '0.00') +
      ' &middot; open: ' + (snapshot.open_count || 0);
  }
  var table = document.getElementById('staff_inwire_tbl');
  var body = table.getElementsByTagName('tbody')[0];
  body.innerHTML = '';
  var selection = document.getElementById('staff_inwire_id');
  selection.options.length = 1;
  var rows = snapshot.inbounds || [];
  for (var i = 0; i < rows.length; i++) {
    var row = body.insertRow(-1);
    row.insertCell(0).innerHTML = rows[i].originator_name;
    row.insertCell(1).innerHTML = '$' + rows[i].amount;
    row.insertCell(2).innerHTML = rows[i].status;
    row.insertCell(3).innerHTML = (rows[i].imad || rows[i].inbound_id).slice(0, 12);
    if (['held', 'queued', 'pending_release', 'unmatched', 'posted'].indexOf(rows[i].status) < 0) {
      continue;
    }
    var option = document.createElement('OPTION');
    option.value = rows[i].inbound_id;
    option.innerHTML = rows[i].originator_name + ' $' + rows[i].amount + ' (' + rows[i].status + ')';
    selection.options.add(option);
  }
}

function postStaffInWire(path, payload) {
  payload.userid = userid;
  payload.customer_id = $('#customer_id_input').val() || document.getElementById('customer_id').innerHTML;
  fetch(homeURL + path, {
    method: 'post',
    body: JSON.stringify(payload),
    headers: { 'Content-type': 'application/json' }
  }).then(function(response) {
    return response.json().then(function(body) {
      var result = document.getElementById('staff_inwire_result');
      if (!response.ok) {
        if (result) { result.innerHTML = (body && (body.message || body.error)) || 'Failed'; }
        return;
      }
      if (result) { result.innerHTML = body.message || 'Done'; }
      getCustomer($('#customer_id_input').val() || document.getElementById('customer_id').innerHTML);
    });
  }).catch(function(error) {
    console.error(error);
  });
}

function postStaffWire(path, payload) {
  payload.userid = userid;
  fetch(homeURL + path, {
    method: 'post',
    body: JSON.stringify(payload),
    headers: { 'Content-type': 'application/json' }
  }).then(function(response) {
    return response.json().then(function(body) {
      var result = document.getElementById('staff_wire_result');
      if (!response.ok) {
        if (result) { result.innerHTML = (body && (body.message || body.error)) || 'Failed'; }
        return;
      }
      if (result) { result.innerHTML = body.message || 'Done'; }
      getCustomer($('#customer_id_input').val() || document.getElementById('customer_id').innerHTML);
    });
  }).catch(function(error) {
    console.error(error);
  });
}

function fillCustomerAccTbl(data){
  var table = document.getElementById('cust_accounts_tbl');
  var rowCount = table.rows.length;
  try {
    for(var i=1; i<rowCount; i++) {
      table.deleteRow(i);
      rowCount--;
      i--;
    }
  }catch(e) {
    alert(e);
  }

  if(data.Accounts.savings != "None"){
    var rowCount = table.rows.length;
    var row = table.insertRow(rowCount);
    var cell1 = row.insertCell(0);
	  cell1.innerHTML = data.Accounts.savings.Account;

	  var cell2 = row.insertCell(1);
	  cell2.innerHTML = 'Savings';

	  var cell3 = row.insertCell(2);
	  cell3.innerHTML = data.Accounts.savings.Balance;
  }
  if(data.Accounts.checkin != "None"){
    var rowCount = table.rows.length;
    var row = table.insertRow(rowCount);
    var cell1 = row.insertCell(0);
	  cell1.innerHTML = data.Accounts.checkin.Account;

	  var cell2 = row.insertCell(1);
	  cell2.innerHTML = 'Savings';

	  var cell3 = row.insertCell(2);
	  cell3.innerHTML = data.Accounts.checkin.Balance;
  }
  if(data.Accounts.credit != "None"){
    var rowCount = table.rows.length;
    var row = table.insertRow(rowCount);
    var cell1 = row.insertCell(0);
	  cell1.innerHTML = data.Accounts.credit.Account;

	  var cell2 = row.insertCell(1);
	  cell2.innerHTML = 'Savings';

	  var cell3 = row.insertCell(2);
	  cell3.innerHTML = data.Accounts.credit.Balance;
  }
}

function approve_request(userid, xactno) {
  console.log("approve request called");

  const approveRequestData = {
    userid : userid,
    transaction_no : xactno
  };

  fetch(homeURL+'approveRequestEmp', {
    method : 'post',
    body : JSON.stringify(approveRequestData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("approveRequest response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Invalid transaction_no'){
      window.alert('Invalid Transaction No input!');
    }
    else if(data.message == 'done'){
      window.alert('Approved!');
    }
    else{
      window.alert(data.message);
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function deny_request(userid, xactno) {
  console.log("deny request called");

  const denyRequestData = {
    userid : userid,
    transaction_no : xactno
  };

  fetch(homeURL+'denyRequest', {
    method : 'post',
    body : JSON.stringify(denyRequestData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("denyRequest response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Invalid transaction_no'){
      window.alert('Invalid Transaction No input!');
    }
    else if(data.message == 'Request Cancelled'){
      window.alert('Denied!');
    }
    else{
      window.alert(data.message);
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function register_customer() {
  console.log("register_customer called");
  a = $('#create_userid').val().toString();
  console.log("i'm, " , a);

  const register_customerData = {
    'userid' : $('#create_userid').val().toString(),
    'empid' : $('#create_userid').val().toString(),
    'password' : $('#create_password').val().toString(),
    'email' : $('#create_email_id').val().toString(),
    'firstname' : $('#create_first_name').val().toString(),
    'midname' : $('#create_middle_name').val().toString(),
    'lastname' : $('#create_last_name').val().toString(),
    'phone' : $('#create_contact_no').val().toString(),
    'dob' : $('#create_dob').val().toString(),
    'ssn' : $('#create_ssn').val().toString(),
    'address' : $('#create_address').val().toString()
  };

  fetch(homeURL+'registerCustomer', {
    method : 'post',
    body : JSON.stringify(register_customerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("register_customer response received");
    return response.json();
  }).then(function (data) {
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

function newAcc(customer_id, account_type) {
  console.log("newAcc called");

  const newAccData = {
    userid : userid,
    customer_id : customer_id,
    account_type : account_type
  };

  fetch(homeURL+'openNewAccount', {
    method : 'post',
    body : JSON.stringify(newAccData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("newAcc response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message[0] == 'Customer already have '){
      window.alert('You already have a '+ data.message[1] + ' account!');
    }
    else if(data.message[0] == 'Done'){
      window.alert('Congratulations! You have a new account with us.');
    }
  }).catch(function(error){
    console.error(error);
  });
}

function modify_customer() {
  console.log("modify_customer called");

  const modify_customerData = {
    userid : userid,
    customer_id : $('#modify_userid').val(),
    last_name : $('#modify_last_name').val(),
    middle_name : $('#modify_middle_name').val(),
    first_name : $('#modify_first_name').val(),
    contact_no : $('#modify_contact_no').val(),
    email_id : $('#modify_email_id').val(),
    ssn : $('#modify_ssn').val(),
    dob : $('#modify_dob').val(),
    address : $('#modify_address').val()
  };

  fetch(homeURL+'modifyCustomer', {
    method : 'post',
    body : JSON.stringify(modify_customerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("modify_customer response received");
    if (response.redirected) {
      localStorage.setItem('loggedStatus', '0');
      window.location.href = response.url;
    }
    else {
      return response.json();
    }
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

function deleteUser(userid, delete_id) {
  console.log("deleteUser called");

  const deleteUserData = {
    userid: userid,
    customer_id: delete_id
  };

  fetch(homeURL+'deactivateCustomer', {
    method : 'post',
    body : JSON.stringify(deleteUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("deleteUser response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

function closeAccount(account_no) {
  console.log("closeAccount called");

  const closeAccountData = {
    userid: userid,
    account_no: account_no
  };

  fetch(homeURL+'deactivateAccount', {
    method : 'post',
    body : JSON.stringify(closeAccountData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("closeAccount response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Done'){
      window.alert('Account closed!');
    }
    else{
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
}

function changePassword(userid, oldPassword, newPassword) {
  const resetpwData = {
    userid : userid,
    oldPassword : oldPassword,
    newPassword : newPassword,
    requester : 'Employee',
    flag : 1
  };

  fetch(homeURL+'resetpPassword', {
    method : 'post',
    body : JSON.stringify(resetpwData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("resetpw response received");
    if(response.redirected){
      localStorage.setItem('loggedStatus', '0');
      window.location.href = response.url;
    }
    else {
      return response.json();
    }
  }).then(function (data) {
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

$(document).ready(function() {
  userid = localStorage.getItem('user');
  usertype = localStorage.getItem('usertype');

    $('#logout_btn').on('click', function(){
      logout();
    });
    $('#account_details_btn').on('click', function(){
      if($('#account_details_pane').css('display')=='none') {
        $('#account_details_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#transactions_btn').on('click', function(){
      if($('#transactions_pane').css('display')=='none') {
        $('#transactions_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','#FF6600');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#cust_accs_btn').on('click', function(){
      getUser();
      if($('#cust_accs_pane').css('display')=='none') {
        $('#cust_accs_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','#FF6600');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#app_dec_transactions_btn').on('click', function(){
      if($('#app_dec_transactions_pane').css('display')=='none') {
        $('#app_dec_transactions_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','#FF6600');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#create_accs_btn').on('click', function(){
      if($('#create_accs_pane').css('display')=='none') {
        $('#create_accs_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','#FF6600');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#modify_accs_btn').on('click', function(){
      if($('#modify_accs_pane').css('display')=='none') {
        $('#modify_accs_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','#FF6600');
    });
    $('#cust_accs_btn').click();
    $(".loader-wrapper").delay( 1000 ).fadeOut("slow");
    $('#home_logo').on('click', function(){
      $('#cust_accs_btn').click();
    });
    $('#update_info_btn').on('click', function(){
      console.log("updateInfo function");
      updateInfo(userid, $('#account_email_id').val(), $('#account_contact_no').val(), $('#account_address').val());
    });
    $('#customer_id_input_btn').on('click', function(){
      if($('#customer_id_input').val() == ''){
        window.alert('No input!');
      }
      else {
        getCustomer($('#customer_id_input').val());
      }
    });
    $('#modify_customer_account_get_btn').on('click', function(){
      if($('#modify_userid').val() == ''){
        window.alert('No input!');
      }
      else {
        getModifyCustomer($('#modify_userid').val());
      }
    });
    $('#customer_id_clear_btn').on('click', function(){
      $('#cust_details_card').hide();
      $('#cust_accounts_tbl').hide();
      $('#staff_linked_card').hide();
      $('#staff_wire_card').hide();
      $('#staff_inwire_card').hide();
    });
    $('#staff_la_force_btn').on('click', function(){
      if($('#staff_la_link_id').val() == 'select'){ window.alert('Select a pending link.'); return; }
      postStaffLinked('forceVerifyLinkedAccount', { link_id: $('#staff_la_link_id').val() });
    });
    $('#staff_la_accept_btn').on('click', function(){
      if($('#staff_la_link_id').val() == 'select'){ window.alert('Select a pending link.'); return; }
      postStaffLinked('acceptPrenote', { link_id: $('#staff_la_link_id').val() });
    });
    $('#staff_la_reject_btn').on('click', function(){
      if($('#staff_la_link_id').val() == 'select'){ window.alert('Select a pending link.'); return; }
      postStaffLinked('rejectPrenote', { link_id: $('#staff_la_link_id').val() });
    });
    $('#staff_la_settle_btn').on('click', function(){
      if($('#staff_la_movement_id').val() == 'select'){ window.alert('Select a sent ACH.'); return; }
      postStaffLinked('settleLinkedAch', { movement_id: $('#staff_la_movement_id').val() });
    });
    $('#staff_la_return_btn').on('click', function(){
      if($('#staff_la_movement_id').val() == 'select'){ window.alert('Select a sent ACH.'); return; }
      postStaffLinked('returnLinkedAch', { movement_id: $('#staff_la_movement_id').val(), reason: 'unauthorized' });
    });
    $('#staff_wire_override_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('overrideOfac', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_wire_release_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('releaseWire', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_wire_reject_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('rejectWire', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_wire_complete_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('completeWire', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_wire_recall_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('recallWire', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_inwire_override_btn').on('click', function(){
      if($('#staff_inwire_id').val() == 'select'){ window.alert('Select an inbound wire.'); return; }
      postStaffInWire('overrideInWireOfac', { inbound_id: $('#staff_inwire_id').val() });
    });
    $('#staff_inwire_release_btn').on('click', function(){
      if($('#staff_inwire_id').val() == 'select'){ window.alert('Select an inbound wire.'); return; }
      postStaffInWire('releaseInWire', { inbound_id: $('#staff_inwire_id').val() });
    });
    $('#staff_inwire_reject_btn').on('click', function(){
      if($('#staff_inwire_id').val() == 'select'){ window.alert('Select an inbound wire.'); return; }
      postStaffInWire('rejectInWire', { inbound_id: $('#staff_inwire_id').val() });
    });
    $('#staff_inwire_return_btn').on('click', function(){
      if($('#staff_inwire_id').val() == 'select'){ window.alert('Select an inbound wire.'); return; }
      postStaffInWire('returnInWire', { inbound_id: $('#staff_inwire_id').val(), reason: 'other' });
    });
    $('#staff_inwire_ingest_btn').on('click', function(){
      if($('#staff_inwire_file').val() == ''){ window.alert('Paste a FAIM file.'); return; }
      postStaffInWire('ingestInWireFile', { file: $('#staff_inwire_file').val() });
    });
    $('#approve_trans_btn').on('click', function(){
      if($('#customer_trans_no').val() == 'select'){
        window.alert('No input!');
      }
      else {
        approve_request(userid, $('#customer_trans_no').val());
      }
    });
    $('#deny_trans_btn').on('click', function(){
      if($('#customer_trans_no').val() == 'select'){
        window.alert('No input!');
      }
      else {
        deny_request(userid, $('#customer_trans_no').val());
      }
    });
    $('#create_customer_form').on('submit', function(e){
      e.preventDefault();
      register_customer();
    });
    $('#create_customer_account_btn').on('click', function(){
      if($('#account_customer_id').val() == ''){
        window.alert('No input!');
      }
      else {
        newAcc($('#account_customer_id').val(), $('#account_account_type').val());
      }
    });
    $('#modify_customer_form').on('submit', function(e){
      e.preventDefault();
      modify_customer();
    });
    $('#delete_customer_id_btn').on('click', function(){
      if($('#delete_customer_id').val() == ''){
        window.alert('No input!');
      }
      else {
        deleteUser(userid, $('#delete_customer_id').val());
      }
    });
    $('#close_account_no_btn').on('click', function(){
      if($('#close_account_no').val() == ''){
        window.alert('No input!');
      }
      else {
        closeAccount($('#close_account_no').val());
      }
    });
    $('#changePW_btn').on('click', function(){
      if($('#changePW_oldPW').val() == '' || $('#changePW_newPW').val() == '' || $('#changePW_confirmPW').val() == '') {
        window.alert("Empty Input!");
      }
      else {
        if($('#changePW_newPW').val() == $('#changePW_oldPW').val()){
          window.alert("Old & New Password shouldn't be the same!");
        }
        else {
          if($('#changePW_newPW').val() == $('#changePW_confirmPW').val()){
            changePassword(userid, $('#changePW_oldPW').val(), $('#changePW_newPW').val());
            $('#changePW_close').click();
          }
          else {
            window.alert("Re-entered password doesn't match new password!");
          }
        }
      }
    });
  });